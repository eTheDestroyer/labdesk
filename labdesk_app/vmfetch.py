"""Download the LabDesk-VMs bundle from the private GitHub repo's release (LabDesk for Windows, first start).

``tools/publish_vm_bundle.py`` uploads the bundle as parts (< 2 GiB, GitHub's limit per release asset) plus
``labdesk-vms.json`` (sizes + SHA-256 of every file and part).  This module:

* finds the newest release of :data:`DEFAULT_REPO` (``$LABDESK_VMS_REPO`` overrides) that has the manifest,
* signs in with the GitHub login Git already has on this PC (``git credential fill`` - Git Credential
  Manager shows its sign-in window the first time), or ``$GH_TOKEN`` / ``$GITHUB_TOKEN`` / ``gh auth token``,
* downloads part by part with resume (HTTP Range), checks each part's SHA-256, appends it to its file and
  deletes it (peak disk use = the bundle + one part), checks the whole file's SHA-256,
* writes ``labdesk-bundle.json`` last: :func:`vmware.find_bundle` sees the bundle only when it is complete.

No password, token or URL with a token is ever logged.  Stdlib only.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config, plat

DEFAULT_REPO = "eTheDestroyer/labdesk-vms"
MANIFEST_ASSET = "labdesk-vms.json"
FORMAT = 1
API = "https://api.github.com"
CHUNK = 1 << 20


class FetchError(RuntimeError):
    pass


def repo() -> str:
    r = os.environ.get("LABDESK_VMS_REPO") or DEFAULT_REPO
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r):
        raise FetchError(f"bad repository name {r!r}")
    return r


def part_name(rel_path: str, n: int) -> str:
    """Release asset name of part ``n`` (1-based) of a bundle file: ``CML/cml.vmdk`` -> ``CML--cml.vmdk.p01``."""
    return rel_path.replace("/", "--") + f".p{n:02d}"


# ------------------------------------------------------------------ GitHub login
def _git_exe() -> str | None:
    g = shutil.which("git")
    if g:
        return g
    if plat.IS_WINDOWS:
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            for sub in (("Git", "cmd", "git.exe"), ("Programs", "Git", "cmd", "git.exe")):
                if base and os.path.exists(os.path.join(base, *sub)):
                    return os.path.join(base, *sub)
    return None


def github_token() -> str:
    for k in ("LABDESK_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    git = _git_exe()
    if git:
        try:
            cp = subprocess.run([git, "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                capture_output=True, text=True, timeout=300, **plat.no_window_kwargs())
            for line in cp.stdout.splitlines():
                if line.startswith("password="):
                    return line.split("=", 1)[1]
        except (OSError, subprocess.TimeoutExpired):
            pass
    gh = shutil.which("gh")
    if gh:
        try:
            cp = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=30,
                                **plat.no_window_kwargs())
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise FetchError("no GitHub login on this PC: install Git for Windows and sign in to GitHub once "
                     "(git clone of LabDesk does that), or set GH_TOKEN")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(url: str, token: str | None, accept: str, rng: int = 0, timeout: float = 60):
    headers = {"Accept": accept, "User-Agent": "LabDesk", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if rng:
        headers["Range"] = f"bytes={rng}-"
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e                       # caller follows Location (without the token)
        if e.code in (401, 403):
            raise FetchError("GitHub refused the login (no access to the private VM repository "
                             f"{repo()}?)") from None
        if e.code == 404:
            raise FetchError(f"{repo()}: not found or no access") from None
        raise FetchError(f"GitHub: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"GitHub unreachable ({getattr(e, 'reason', e)})") from None


def _json(path: str, token: str):
    with _open(API + path, token, "application/vnd.github+json") as r:
        return json.loads(r.read())


@dataclass
class Remote:
    tag: str
    manifest: dict
    assets: dict          # name -> asset id

    @property
    def total(self) -> int:
        return int(self.manifest.get("total") or 0)


def find_remote(token: str | None = None) -> Remote:
    token = token or github_token()
    for rel in _json(f"/repos/{repo()}/releases?per_page=20", token):
        assets = {a["name"]: a["id"] for a in rel.get("assets", [])}
        if MANIFEST_ASSET in assets:
            data = _download_small(assets[MANIFEST_ASSET], token)
            mf = json.loads(data)
            if mf.get("format") != FORMAT:
                continue
            missing = [p["name"] for f in mf["files"] for p in f["parts"] if p["name"] not in assets]
            if missing:
                raise FetchError(f"release {rel['tag_name']} is incomplete ({len(missing)} parts missing)")
            return Remote(rel["tag_name"], mf, assets)
    raise FetchError(f"no LabDesk VM release in {repo()} yet (upload it with tools/publish_vm_bundle.py)")


def _asset_response(asset_id: int, token: str, rng: int = 0):
    r = _open(f"{API}/repos/{repo()}/releases/assets/{asset_id}", token, "application/octet-stream", rng)
    if getattr(r, "code", 200) in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location")
        r.close()
        if not loc or urllib.parse.urlsplit(loc).scheme != "https":
            raise FetchError("GitHub sent no download location")
        r = _open(loc, None, "application/octet-stream", rng, timeout=120)
    return r


def _download_small(asset_id: int, token: str) -> bytes:
    with _asset_response(asset_id, token) as r:
        return r.read()


# ------------------------------------------------------------------ download
def target_dir() -> Path:
    """Where the bundle goes: inside the git folder (git ignores it), next to LabDesk.exe, else the home folder."""
    from .vmware import BUNDLE_DIR_NAME
    cands = []
    if plat.FROZEN:
        cands.append(Path(sys.executable).resolve().parent / BUNDLE_DIR_NAME)
    else:
        cands.append(config.APP_DIR / BUNDLE_DIR_NAME)
    cands.append(Path.home() / BUNDLE_DIR_NAME)
    for c in cands:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return c
        except OSError:
            continue
    raise FetchError("no writable folder for the VMs")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


class Progress:
    def __init__(self, total: int, log, every_s: float = 20.0):
        self.total, self.log, self.every = max(1, total), log, every_s
        self.done = 0
        self.t0 = self.last = time.time()

    def add(self, n: int, force: bool = False) -> None:
        self.done += n
        now = time.time()
        if force or now - self.last >= self.every:
            self.last = now
            rate = self.done / max(1.0, now - self.t0)
            left = (self.total - self.done) / rate if rate > 0 else 0
            self.log(f"downloading the LabDesk VMs: {self.done / self.total * 100:.0f} % of "
                     f"{self.total / 1e9:.1f} GB ({rate / 1e6:.1f} MB/s, about {left / 60:.0f} min left)")


def fetch(log=print, remote: Remote | None = None, token: str | None = None, dest: Path | None = None,
          stop=None) -> Path:
    """Download (or finish downloading) the bundle; returns its folder.  ``stop()`` -> True aborts (resumable)."""
    from .vmware import MANIFEST
    token = token or github_token()
    remote = remote or find_remote(token)
    root = Path(dest) if dest else target_dir()
    work = root / ".download"
    work.mkdir(parents=True, exist_ok=True)
    state_file = work / "done.json"
    try:
        done = json.loads(state_file.read_text(encoding="utf-8"))
        if done.get("tag") != remote.tag:
            done = {"tag": remote.tag, "files": {}}
    except (OSError, ValueError):
        done = {"tag": remote.tag, "files": {}}

    need = sum(f["size"] for f in remote.manifest["files"] if done["files"].get(f["path"]) != f["sha256"])
    free = shutil.disk_usage(root).free
    biggest = max((p["size"] for f in remote.manifest["files"] for p in f["parts"]), default=0)
    if free < need + biggest + (1 << 30):
        raise FetchError(f"not enough disk space on {root.anchor or root}: {need / 1e9:.1f} GB needed, "
                         f"{free / 1e9:.1f} GB free")
    log(f"downloading the LabDesk VMs ({remote.tag}, {need / 1e9:.1f} GB) into {root}…")
    prog = Progress(need, log)
    for f in remote.manifest["files"]:
        final = root / Path(*f["path"].split("/"))
        if done["files"].get(f["path"]) == f["sha256"] and final.exists() and final.stat().st_size == f["size"]:
            continue
        final.parent.mkdir(parents=True, exist_ok=True)
        building = work / (f["path"].replace("/", "--") + ".building")
        # parts already appended to the file being built (resume after a break)
        have = building.stat().st_size if building.exists() else 0
        offset, start_part = 0, 0
        for i, p in enumerate(f["parts"]):
            if offset + p["size"] <= have:
                offset += p["size"]
                start_part = i + 1
            else:
                break
        if have != offset:
            with open(building, "r+b") as fh:
                fh.truncate(offset)
        prog.add(offset)
        for p in f["parts"][start_part:]:
            if stop and stop():
                raise FetchError("download paused - it continues where it stopped next time")
            part_file = work / p["name"]
            _download_part(remote.assets[p["name"]], token, part_file, p, prog, stop)
            with open(building, "ab") as out, open(part_file, "rb") as fin:
                shutil.copyfileobj(fin, out, 8 << 20)
            part_file.unlink()
        if _sha256(building) != f["sha256"]:
            building.unlink()
            raise FetchError(f"{f['path']}: checksum mismatch after download - start again to retry")
        os.replace(building, final)
        done["files"][f["path"]] = f["sha256"]
        state_file.write_text(json.dumps(done), encoding="utf-8")
    (root / MANIFEST).write_text(json.dumps(remote.manifest["bundle"], indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    prog.add(0, force=True)
    log(f"LabDesk VMs ready in {root}")
    return root


def _download_part(asset_id: int, token: str, dest: Path, part: dict, prog: Progress, stop=None) -> None:
    for attempt in range(1, 6):
        have = dest.stat().st_size if dest.exists() else 0
        if have > part["size"]:
            dest.unlink()
            have = 0
        try:
            if have < part["size"]:
                with _asset_response(asset_id, token, rng=have) as r, open(dest, "ab" if have else "wb") as out:
                    if have and getattr(r, "status", 206) != 206:      # server ignored Range: start over
                        out.truncate(0)
                        prog.add(-have)
                    while chunk := r.read(CHUNK):
                        out.write(chunk)
                        prog.add(len(chunk))
                        if stop and stop():
                            raise FetchError("download paused - it continues where it stopped next time")
            if _sha256(dest) == part["sha256"]:
                return
            prog.add(-dest.stat().st_size)
            dest.unlink()
        except (OSError, FetchError, http.client.HTTPException) as e:
            if "paused" in str(e) or attempt == 5:
                raise FetchError(str(e)) from None
            time.sleep(5 * attempt)
    raise FetchError(f"{part['name']}: download failed")
