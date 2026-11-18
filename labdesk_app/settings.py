"""The one place LabDesk opens its persisted settings (L1).

``QSettings(IniFormat, UserScope, 'LabDesk', 'LabDesk')`` lives in ``$XDG_CONFIG_HOME/LabDesk/LabDesk.ini``;
the pre-L1 ``QSettings('LabDesk', 'LabDesk')`` (NativeFormat) used ``LabDesk.conf`` next to it, so the first
call copies those keys over once (nothing the user already chose is lost; the old file is left as is).  The QA tools set ``XDG_CONFIG_HOME`` (or ``QSettings.setPath``) to a temp dir
before the QApplication exists, so screenshots never write to the user's ~/.config/LabDesk.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings

ORG = APP = "LabDesk"
STATE_VERSION = 3            # bump when the dock layout changes so old saveState blobs are not restored


_MIGRATED: list = []


def app_settings() -> QSettings:
    st = QSettings(QSettings.IniFormat, QSettings.UserScope, ORG, APP)
    if not _MIGRATED:
        _MIGRATED.append(True)
        try:
            _migrate_native(st)
        except Exception:  # noqa: BLE001  (never block startup on an old settings file)
            pass
    return st


def _migrate_native(st: QSettings) -> None:
    from pathlib import Path
    ini = Path(st.fileName())
    old = ini.with_suffix(".conf")
    if ini.exists() or not old.exists() or st.allKeys():
        return
    src = QSettings(str(old), QSettings.IniFormat)
    for k in src.allKeys():
        st.setValue(k, src.value(k))
    st.sync()


def as_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("1", "true", "yes", "on")
    if v is None:
        return default
    return bool(v)
