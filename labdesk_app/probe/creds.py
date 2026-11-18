"""Device credentials for probe bursts (D1 step 3).

File: ``~/.config/labdesk/device-creds`` (override: $LABDESK_DEVICE_CREDS), KEY=VALUE, mode 0600.
Never state.json, never logged or displayed.

    DEFAULT_USERNAME=admin        # sent only when a 'Username:' prompt actually appears
    DEFAULT_PASSWORD=...          # sent only when a login 'Password:' prompt appears
    DEFAULT_ENABLE=...            # sent only when a burst needs privilege 15 and the line is at '>'
    R1_ENABLE=...                 # per-node override (CML label, upper-case, non-alnum -> '_')

LabDesk writes this file itself (right-click a device -> Credentials...) through :func:`save`: atomic replace,
mode 0600, every other line (keys of other devices, comments, blank lines) kept as it was.

Missing file -> no credentials: read-only bursts still run at '>' (IOS show commands used by
the FAST/SLOW tiers work at privilege 1); privilege-15 commands fail with 'needs enable secret'.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import config, plat


def creds_path() -> Path:
    return Path(os.environ.get("LABDESK_DEVICE_CREDS", str(config.CONFIG_DIR / "device-creds")))


@dataclass(frozen=True)
class DeviceCreds:
    username: str | None = None
    password: str | None = None
    enable: str | None = None

    def __repr__(self) -> str:                    # never print values
        return (f"DeviceCreds(username={'set' if self.username else None}, "
                f"password={'set' if self.password else None}, enable={'set' if self.enable else None})")


class CredsError(RuntimeError):
    pass


def _key(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", label).upper()


def load(label: str, path: Path | None = None) -> DeviceCreds:
    p = Path(path) if path else creds_path()
    if not p.exists():
        return DeviceCreds()
    st = p.stat()
    # Windows: st_mode always looks like 0o666 - the per-user profile's ACLs protect the file instead
    if not plat.IS_WINDOWS and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredsError(f"{p} must be mode 0600 (chmod 600 {p})")
    vals = config.load_env_file(p)
    k = _key(label)

    def get(name):
        return vals.get(f"{k}_{name}") or vals.get(f"DEFAULT_{name}") or None

    return DeviceCreds(get("USERNAME"), get("PASSWORD"), get("ENABLE"))


def all_secret_values(path: Path | None = None) -> list[str]:
    """Every secret value in the file (for literal redaction of transcripts)."""
    p = Path(path) if path else creds_path()
    out = []
    try:
        for k, v in config.load_env_file(p).items():
            if k.endswith(("_PASSWORD", "_ENABLE")) and v:
                out.append(v)
    except OSError:
        pass
    try:
        for k, v in config.load_env_file(config.CRED_FILE).items():
            if "PASS" in k and v:
                out.append(v)
    except OSError:
        pass
    return out


# ----------------------------------------------------------------------------- per-node detail + writer
FIELDS = ("USERNAME", "PASSWORD", "ENABLE")


@dataclass(frozen=True)
class CredsDetail:
    """What the Credentials dialog and the hover card need: the effective values and where each came from."""
    creds: DeviceCreds
    own: dict                      # FIELD -> value stored for this node (only the fields that exist)
    defaults: dict                 # FIELD -> DEFAULT_* value
    error: str | None = None       # e.g. the file has the wrong mode

    @property
    def uses_defaults(self) -> bool:
        """No per-node value at all: the node runs on DEFAULT_*."""
        return not self.own

    def source(self, fld: str) -> str | None:
        """'device' | 'default' | None."""
        if self.own.get(fld):
            return "device"
        if self.defaults.get(fld):
            return "default"
        return None

    def __repr__(self) -> str:     # never print values
        return f"CredsDetail(own={sorted(self.own)}, defaults={sorted(self.defaults)}, error={self.error!r})"


def node_key(label: str) -> str:
    """The key prefix of a node label ('R1' -> 'R1', 'core-sw.1' -> 'CORE_SW_1')."""
    return _key(label)


def load_detail(label: str, path: Path | None = None) -> CredsDetail:
    p = Path(path) if path else creds_path()
    try:
        creds = load(label, p)
    except CredsError as e:
        return CredsDetail(DeviceCreds(), {}, {}, str(e))
    except OSError as e:
        return CredsDetail(DeviceCreds(), {}, {}, f"cannot read {p}: {e.strerror}")
    try:
        vals = config.load_env_file(p) if p.exists() else {}
    except OSError:
        vals = {}
    k = _key(label)
    own = {f: vals[f"{k}_{f}"] for f in FIELDS if vals.get(f"{k}_{f}")}
    dfl = {f: vals[f"DEFAULT_{f}"] for f in FIELDS if vals.get(f"DEFAULT_{f}")}
    return CredsDetail(creds, own, dfl)


def check_value(fld: str, value: str | None) -> str | None:
    """Why ``value`` cannot be stored in the KEY=VALUE file (None = fine)."""
    if not value:
        return None
    if any(c in value for c in "\r\n\0"):
        return "line breaks are not allowed"
    if value != value.strip():
        return "no spaces at the start or end"
    if value[0] in "\"'" or value[-1] in "\"'":
        return "cannot start or end with a quote"
    if fld == "USERNAME" and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", value):
        return "user names use letters, digits, '_', '.', '@', '-'"
    return None


def save(label: str | None, values: dict, path: Path | None = None, *, use_defaults: bool = False) -> Path:
    """Store credentials for ``label`` (or the DEFAULT_* set when ``label`` is None).

    ``values``: {'USERNAME'|'PASSWORD'|'ENABLE': str|None}; an empty / None value removes that key.
    ``use_defaults=True`` removes every per-node key of ``label`` (the node then uses DEFAULT_*), and
    ``values`` (if any) are written as the DEFAULT_* set.

    Atomic: written to a 0600 temp file in the same directory, fsync'ed, then ``os.replace``d; every other
    line of the file (other devices, comments, blank lines) is kept verbatim.  Values are never logged."""
    p = Path(path) if path else creds_path()
    for f, v in values.items():
        if f not in FIELDS:
            raise ValueError(f"unknown field {f}")
        err = check_value(f, v)
        if err:
            raise ValueError(f"{f.lower()}: {err}")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        old = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        old = ["# LabDesk device credentials (KEY=VALUE, mode 0600) - see labdesk_app/probe/creds.py"]
    node = _key(label) if label else None
    if use_defaults or node is None:
        updates = {f"DEFAULT_{f}": (v or None) for f, v in values.items()}
        if node is not None:
            updates.update({f"{node}_{f}": None for f in FIELDS})
    else:
        updates = {f"{node}_{f}": (v or None) for f, v in values.items()}
    out, done = [], set()
    for line in old:
        st = line.strip()
        k = st.split("=", 1)[0].strip() if st and not st.startswith("#") and "=" in st else None
        if k is not None and k in updates:
            if k in done:
                continue                    # drop duplicates of a key we rewrite
            done.add(k)
            if updates[k] is not None:
                out.append(f"{k}={updates[k]}")
            continue
        out.append(line)
    for k, v in updates.items():
        if k not in done and v is not None:
            out.append(f"{k}={v}")
    data = ("\n".join(out).rstrip("\n") + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".device-creds.", dir=str(p.parent))
    try:
        if not plat.IS_WINDOWS:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    plat.harden_private_file(p)
    return p


def mtime(path: Path | None = None) -> float | None:
    """Modification time of the creds file (None = missing): callers re-read their secret caches on change."""
    try:
        return (Path(path) if path else creds_path()).stat().st_mtime_ns / 1e9
    except OSError:
        return None
