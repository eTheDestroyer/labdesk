"""Paths, settings and credential loading.

Secrets are only ever read from the KEY=VALUE env file; they are never logged,
displayed or passed on a command line.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import plat

APP_DIR = plat.resource_dir()                             # ~/cml-labs/labdesk (or the unpacked LabDesk.exe)
PKG_DIR = APP_DIR / "labdesk_app"
TEMPLATES_DIR = APP_DIR / "templates"
ICON_PATH = APP_DIR / "labdesk.svg"

CML_VM_NAME = os.environ.get("LABDESK_CML_VM", "cml")
LIBVIRT_URI = os.environ.get("LABDESK_LIBVIRT_URI", "qemu:///system")
# Linux: ~/cml-labs/cml-credentials.env (install.sh); Windows: %APPDATA%\LabDesk\cml-credentials.env
CRED_FILE = Path(os.path.expanduser(os.environ["CML_CRED_FILE"])) if os.environ.get("CML_CRED_FILE") \
    else plat.default_cred_file()

SSH_KEY = Path(os.path.expanduser("~/.ssh/labdesk_ed25519"))
KNOWN_HOSTS = Path(os.path.expanduser("~/.ssh/labdesk_known_hosts"))
DEVICE_KNOWN_HOSTS = Path(os.path.expanduser("~/.ssh/labdesk_devices_known_hosts"))

CONFIG_DIR = plat.default_config_dir()                    # ~/.config/labdesk | %APPDATA%\LabDesk
STATE_FILE = CONFIG_DIR / "state.json"
CACHE_DIR = plat.default_cache_dir()                      # ~/.cache/labdesk | %LOCALAPPDATA%\LabDesk\cache

# host-KVM images (Downloads); base volumes are uploaded once into the libvirt pool
KVM_POOL = "images"
VJUNOS_IMAGE = Path(os.path.expanduser("~/Downloads/vJunos-router-26.2R1.7.qcow2"))
FORTIOS_ZIP = Path(os.path.expanduser("~/Downloads/FFW_VM64_KVM-v7.6.8.M-build3744-FORTINET.out.kvm.zip"))

POLL_INTERVAL_MS = 3000
CML_FREE_NODE_LIMIT = 5


def load_env_file(path: Path | str = CRED_FILE) -> dict[str, str]:
    """Parse a KEY=VALUE file (comments/blank lines ignored, optional quotes stripped)."""
    vals: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _cml_host() -> str:
    """$CML_HOST, else CML_HOST= in the credentials file (written by install.sh), else the default."""
    if os.environ.get("CML_HOST"):
        return os.environ["CML_HOST"]
    try:
        return load_env_file(CRED_FILE).get("CML_HOST") or "192.168.122.60"
    except OSError:
        return "192.168.122.60"


CML_HOST = _cml_host()


def reload_cml_host() -> str:
    """Re-read the CML address after the connection dialog saved a new one (module attribute updated)."""
    global CML_HOST
    CML_HOST = _cml_host()
    return CML_HOST


def needs_setup(path: Path | str | None = None) -> bool:
    """True when the credentials file is missing or has no CML_ADMIN_PASS (first run)."""
    try:
        return not load_env_file(path or CRED_FILE).get("CML_ADMIN_PASS")
    except OSError:
        return True


def _env_quote(v: str) -> str:
    # load_env_file strips surrounding whitespace, then quotes: keep leading/trailing spaces inside quotes
    if v != v.strip():
        return "'" + v + "'" if "'" not in v else '"' + v + '"'
    return v


def update_env_file(updates: dict[str, str | None], path: Path | str | None = None) -> Path:
    """Set / remove KEY=VALUE lines in the credentials file (other lines kept), atomically, owner-only."""
    import tempfile
    p = Path(path or CRED_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = ["# LabDesk: CML login (written by LabDesk; keep this file private)"]
    out, done = [], set()
    for line in lines:
        st = line.strip()
        k = st.split("=", 1)[0].strip() if st and not st.startswith("#") and "=" in st else None
        if k is not None and k in updates:
            if k not in done and updates[k] is not None:
                out.append(f"{k}={_env_quote(str(updates[k]))}")
            done.add(k)
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in done and v is not None:
            out.append(f"{k}={_env_quote(str(v))}")
    data = ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".cml-credentials.", dir=str(p.parent))
    try:
        if not plat.IS_WINDOWS:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    plat.harden_private_file(p)
    return p


class State:
    """Small JSON store for things CML does not keep: host-KVM devices placed on a lab's
    canvas, their links, optional management IPs per node, last opened lab."""

    def __init__(self, path: Path = STATE_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict = {"labs": {}, "last_lab": None}
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    def lab(self, lab_id: str) -> dict:
        with self._lock:
            lab = self.data.setdefault("labs", {}).setdefault(lab_id, {})
            lab.setdefault("kvm", {})          # name -> {kind, x, y}
            lab.setdefault("kvm_links", [])    # [{a, b, net, connector?}]
            lab.setdefault("mgmt", {})         # node key -> {ip, user}
            return lab

    @property
    def last_lab(self) -> str | None:
        return self.data.get("last_lab")

    @last_lab.setter
    def last_lab(self, lab_id: str | None) -> None:
        self.data["last_lab"] = lab_id
        self.save()
