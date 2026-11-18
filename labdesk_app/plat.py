"""Platform layer: the one place that knows whether LabDesk runs on Linux or Windows.

Linux behaviour is the reference; on Windows the Linux-only parts (Konsole/D-Bus consoles, libvirt/KVM,
the Internet-cloud network namespace) are replaced (Windows Terminal + OpenSSH, paramiko probes,
VMware ``vmrun``) or switched off with a clear "Linux only" message.

Named ``plat`` (not ``platform``) so it can never shadow the standard-library module.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = bool(getattr(sys, "frozen", False))          # PyInstaller LabDesk.exe

# features that need the Linux host stack
HAS_KVM = not IS_WINDOWS             # libvirt / virsh host VMs (Juniper, Fortinet), "virsh start cml"
HAS_CLOUD = not IS_WINDOWS           # Internet cloud: netns + systemd units
LINUX_ONLY_TIP = "Linux only: needs libvirt/KVM on the host. Not available in LabDesk for Windows."
CLOUD_LINUX_ONLY_TIP = ("Linux only: the Internet cloud is a network namespace on a Linux host. "
                        "Not available in LabDesk for Windows.")

# subprocess flag: never flash a console window from the windowed LabDesk.exe
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


def no_window_kwargs() -> dict:
    """Extra ``subprocess`` kwargs for a background (captured) command: no console window on Windows."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


def resource_dir() -> Path:
    """Folder holding templates/, labdesk.svg and labdesk_app/ (the repo root, or PyInstaller's unpack dir)."""
    if FROZEN and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- per-user folders
def appdata_dir() -> Path:
    """Windows: %APPDATA%\\LabDesk (roaming: credentials, settings, state)."""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return Path(base) / "LabDesk"


def localappdata_dir() -> Path:
    """Windows: %LOCALAPPDATA%\\LabDesk (cache, probe logs)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return Path(base) / "LabDesk"


def default_cred_file() -> Path:
    if IS_WINDOWS:
        return appdata_dir() / "cml-credentials.env"
    return Path(os.path.expanduser("~/cml-labs/cml-credentials.env"))


def default_config_dir() -> Path:
    if IS_WINDOWS:
        return appdata_dir()
    return Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))) / "labdesk"


def default_cache_dir() -> Path:
    if IS_WINDOWS:
        return localappdata_dir() / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))) / "labdesk"


# --------------------------------------------------------------------------- Windows tools
WIN_OPENSSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


def ssh_exe() -> str:
    """The ssh client: plain 'ssh' on Linux; the built-in Windows OpenSSH client on Windows."""
    if not IS_WINDOWS:
        return "ssh"
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    builtin = os.path.join(root, "System32", "OpenSSH", "ssh.exe")
    if os.path.exists(builtin):
        return builtin
    return shutil.which("ssh") or builtin


def windows_terminal_exe() -> str | None:
    """wt.exe (Windows Terminal's app-execution alias) when installed."""
    p = shutil.which("wt") or shutil.which("wt.exe")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cand = os.path.join(local, "Microsoft", "WindowsApps", "wt.exe")
        if os.path.exists(cand):
            return cand
    return None


def vmrun_exe() -> str | None:
    """VMware Workstation's vmrun.exe (PATH, then the default install folders)."""
    p = shutil.which("vmrun")
    if p:
        return p
    if IS_WINDOWS:
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                cand = os.path.join(base, "VMware", "VMware Workstation", "vmrun.exe")
                if os.path.exists(cand):
                    return cand
    return None


def harden_private_file(path: Path) -> None:
    """Owner-only file: chmod 600 on Linux; on Windows %APPDATA% is already per-user (ACL inherited)."""
    if not IS_WINDOWS:
        os.chmod(path, 0o600)
