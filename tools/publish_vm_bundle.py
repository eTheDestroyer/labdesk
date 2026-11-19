#!/usr/bin/env python3
"""Upload the LabDesk-VMs folder to a PRIVATE GitHub repo's release, so LabDesk on Windows downloads it itself.

    cd ~/cml-labs/labdesk
    .venv/bin/python tools/make_windows_bundle.py            # builds ~/LabDesk-VMs (once / after changes)
    .venv/bin/python tools/publish_vm_bundle.py              # uploads it (resumable: run again after a break)

* GitHub release assets must be < 2 GiB: every file is cut into parts of 1900 MiB (``<path>.pNN``).
* ``labdesk-vms.json`` (uploaded LAST, so a half-finished upload is never offered) lists every file with its
  size and SHA-256 and every part with its SHA-256; LabDesk checks all of them while downloading.
* NOT uploaded: cml-credentials.env (your CML password - LabDesk asks for it once on Windows), installer/
  (VMware's own licence), devices/ (VMs made on a PC).
* The target repo must be PRIVATE (licensed Cisco / Juniper / Fortinet images): the script refuses a public one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from labdesk_app import vmfetch, vmware  # noqa: E402

PART = 1900 * 1024 * 1024
SKIP_TOP = {"cml-credentials.env", "installer", "devices", ".work", ".download"}


def gh(*args: str, check: bool = True, timeout: float = 7200) -> str:
    cp = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:3])} failed: {(cp.stderr or cp.stdout).strip()}")
    return cp.stdout


def files_of(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or rel.parts[0] in SKIP_TOP or rel.name.startswith(".") or rel.name == vmware.MANIFEST:
            continue
        if p.suffix.lower() in (".lck", ".log", ".nvram", ".vmsd", ".vmss") or "labdesk-tmp" in p.name:
            continue
        out.append(p)
    return out


def describe(root: Path, path: Path, part_size: int) -> dict:
    rel = path.relative_to(root).as_posix()
    size = path.stat().st_size
    whole = hashlib.sha256()
    parts = []
    with open(path, "rb") as fh:
        n = 0
        while True:
            h = hashlib.sha256()
            left, got = part_size, 0
            while left:
                chunk = fh.read(min(left, 8 << 20))
                if not chunk:
                    break
                h.update(chunk)
                whole.update(chunk)
                got += len(chunk)
                left -= len(chunk)
            if not got and n:
                break
            n += 1
            parts.append({"name": vmfetch.part_name(rel, n), "size": got, "sha256": h.hexdigest()})
            if got < part_size:
                break
    return {"path": rel, "size": size, "sha256": whole.hexdigest(), "parts": parts}


def write_part(path: Path, index: int, size: int, part_size: int, dest: Path) -> None:
    with open(path, "rb") as fin, open(dest, "wb") as fout:
        fin.seek((index - 1) * part_size)
        left = size
        while left:
            chunk = fin.read(min(left, 8 << 20))
            if not chunk:
                break
            fout.write(chunk)
            left -= len(chunk)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", default=str(Path.home() / vmware.BUNDLE_DIR_NAME))
    ap.add_argument("--repo", default=vmfetch.DEFAULT_REPO)
    ap.add_argument("--tag", default=None, help="release tag (default: vms-<bundle build date>)")
    args = ap.parse_args()
    root = Path(args.bundle).expanduser().resolve()
    bundle = json.loads((root / vmware.MANIFEST).read_text(encoding="utf-8"))

    vis = gh("repo", "view", args.repo, "--json", "visibility", "-q", ".visibility").strip()
    if vis != "PRIVATE":
        raise SystemExit(f"{args.repo} is {vis or 'unknown'}: the VM images may only go to a PRIVATE repo")
    tag = args.tag or "vms-" + time.strftime("%Y%m%d", time.strptime(bundle["built"], "%Y-%m-%d %H:%M"))

    print(f"== hashing {root} (a few minutes)…", flush=True)
    entries = [describe(root, p, PART) for p in files_of(root)]
    total = sum(e["size"] for e in entries)
    manifest = {"format": vmfetch.FORMAT, "tag": tag, "built": bundle.get("built"), "part_size": PART,
                "total": total, "bundle": bundle, "files": entries}
    print(f"== {len(entries)} files, {total / 1e9:.1f} GB, "
          f"{sum(len(e['parts']) for e in entries)} parts -> {args.repo} release {tag}", flush=True)

    if gh("release", "view", tag, "--repo", args.repo, "--json", "tagName", check=False).strip() == "":
        gh("release", "create", tag, "--repo", args.repo, "--title", f"LabDesk VMs {bundle.get('built')}",
           "--notes", "VM images for LabDesk on Windows (VMware). Downloaded by LabDesk itself. PRIVATE - licensed "
                      "Cisco / Juniper / Fortinet images.")
    have = {a["name"]: a["size"] for a in json.loads(
        gh("release", "view", tag, "--repo", args.repo, "--json", "assets"))["assets"]}

    done_bytes = 0
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="labdesk-vms-", dir=str(root.parent)) as tmp:
        for e in entries:
            src = root / e["path"]
            for i, part in enumerate(e["parts"], 1):
                if have.get(part["name"]) == part["size"]:
                    print(f"   {part['name']}: already uploaded", flush=True)
                    done_bytes += part["size"]
                    continue
                dest = Path(tmp) / part["name"]
                write_part(src, i, part["size"], PART, dest)
                t1 = time.time()
                for attempt in range(1, 4):
                    cp = subprocess.run(["gh", "release", "upload", tag, str(dest), "--repo", args.repo, "--clobber"],
                                        capture_output=True, text=True, timeout=4 * 3600)
                    if cp.returncode == 0:
                        break
                    print(f"   {part['name']}: upload failed ({cp.stderr.strip()[:200]}), retry {attempt}", flush=True)
                    time.sleep(10 * attempt)
                else:
                    raise SystemExit(f"upload of {part['name']} failed 3 times - run the script again to resume")
                dest.unlink()
                done_bytes += part["size"]
                rate = part["size"] / max(1.0, time.time() - t1) / 1e6
                print(f"   {part['name']}: uploaded ({part['size'] / 1e6:.0f} MB at {rate:.1f} MB/s) - "
                      f"{done_bytes / total * 100:.0f} % of {total / 1e9:.1f} GB", flush=True)
        mf = Path(tmp) / vmfetch.MANIFEST_ASSET
        mf.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        gh("release", "upload", tag, str(mf), "--repo", args.repo, "--clobber")
    print(f"== done in {(time.time() - t0) / 60:.0f} min: https://github.com/{args.repo}/releases/tag/{tag}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
