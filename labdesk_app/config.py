"""Paths, settings and credential loading.

Secrets are only ever read from the KEY=VALUE env file; they are never logged,
displayed or passed on a command line.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # ~/cml-labs/labdesk
PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
ICON_PATH = APP_DIR / "labdesk.svg"

CML_VM_NAME = os.environ.get("LABDESK_CML_VM", "cml")
LIBVIRT_URI = os.environ.get("LABDESK_LIBVIRT_URI", "qemu:///system")
CRED_FILE = Path(os.path.expanduser(os.environ.get("CML_CRED_FILE", "~/cml-labs/cml-credentials.env")))

SSH_KEY = Path(os.path.expanduser("~/.ssh/labdesk_ed25519"))
KNOWN_HOSTS = Path(os.path.expanduser("~/.ssh/labdesk_known_hosts"))
DEVICE_KNOWN_HOSTS = Path(os.path.expanduser("~/.ssh/labdesk_devices_known_hosts"))

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))) / "labdesk"
STATE_FILE = CONFIG_DIR / "state.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "labdesk"

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
