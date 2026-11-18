"""Console / SSH / Telnet / HTTP launchers.

Console commands are argv lists that run on a REAL pty inside Konsole:

* CML node  -> ssh -t <admin>@<controller> connect <console-uuid>
               (CML terminal server; pubkey auth with ~/.ssh/labdesk_ed25519, password
               fallback through an SSH_ASKPASS helper that reads the env file - the
               password never appears on a command line or in Konsole)
* host VM   -> virsh -c qemu:///system console <vm> --force

Windows (LabDesk.exe): one tab per device in a Windows Terminal window named "LabDesk"
(``wt -w LabDesk new-tab``), or a new console window when Windows Terminal is missing; the
command is the built-in OpenSSH client.  No askpass there: the user types the CML password in
the console unless ~/.ssh/labdesk_ed25519 exists.

Konsole: ONE dedicated window (``konsole --separate --profile LabDesk``), one tab per
device, driven over D-Bus. The LabDesk profile's command is ``tabexec.py``: it reads one
JSON line ({argv, env, title}) with echo off and runs that argv on the tab's pty - no user
shell, no shell parsing, no history pollution.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, plat, validate

ASKPASS = config.PKG_DIR / "askpass.py"
TABEXEC = config.PKG_DIR / "tabexec.py"
PROFILE_NAME = "LabDesk"


@dataclass
class ConsoleCommand:
    title: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps({"title": self.title, "argv": self.argv, "env": self.env},
                          separators=(",", ":"))


# ------------------------------------------------------------------ builders
def _known_hosts_opt(path: Path) -> list[str]:
    """``-o UserKnownHostsFile=<path>``.  Windows: forward slashes; a path with blanks is left out (ssh
    would split it into several files, and Windows Terminal re-quotes the command line): ssh then uses
    its default ~/.ssh/known_hosts."""
    if not plat.IS_WINDOWS:
        return ["-o", f"UserKnownHostsFile={path}"]
    p = Path(path).as_posix()
    return [] if any(c.isspace() for c in p) else ["-o", f"UserKnownHostsFile={p}"]


def cml_ssh_base(user: str, host: str | None = None, key: Path = config.SSH_KEY) -> list[str]:
    host = host or config.CML_HOST             # read at call time (connection dialog)
    argv = [plat.ssh_exe(), "-t",
            "-e", "none",                                   # no ~ escapes: raw passthrough
            *_known_hosts_opt(config.KNOWN_HOSTS),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30",
            "-o", "LogLevel=ERROR",
            "-o", "PreferredAuthentications=publickey,password,keyboard-interactive"]
    if Path(key).exists():
        argv += ["-o", "IdentitiesOnly=yes", "-i", str(key)]
    argv.append(f"{validate.username(user)}@{host}")
    return argv


def askpass_env(cred_key: str = "CML_ADMIN_PASS") -> dict[str, str]:
    """Environment for the password fallback; contains only the helper path + key NAME."""
    return {"SSH_ASKPASS": str(ASKPASS), "SSH_ASKPASS_REQUIRE": "force",
            "LABDESK_ASKPASS_KEY": cred_key, "DISPLAY": os.environ.get("DISPLAY", ":0")}


def cml_console(label: str, console_uuid: str, user: str) -> ConsoleCommand:
    argv = cml_ssh_base(user) + ["connect", validate.uuid(console_uuid)]
    # Windows: no askpass helper (it is a Python script) - ssh asks for the CML password in the console
    return ConsoleCommand(validate.cml_label(label), argv, {} if plat.IS_WINDOWS else askpass_env())


def kvm_console(vm: str) -> ConsoleCommand:
    from .kvm import build_console_cmd
    return ConsoleCommand(vm, build_console_cmd(vm), {})


LEGACY_SSH_OPTS = ["-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1",
                   "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]


def ssh_to_device(label: str, ip: str, user: str = "admin") -> ConsoleCommand:
    argv = [plat.ssh_exe(), *_known_hosts_opt(config.DEVICE_KNOWN_HOSTS),
            "-o", "StrictHostKeyChecking=no", "-o", "PubkeyAuthentication=no",
            *LEGACY_SSH_OPTS, "-l", validate.username(user), validate.host(ip)]
    return ConsoleCommand(f"{label} (ssh)", argv, {})


def telnet_to_device(label: str, ip: str) -> ConsoleCommand:
    host = validate.host(ip)
    if plat.IS_WINDOWS:
        exe = shutil.which("telnet")
        if not exe:
            raise RuntimeError("the Windows Telnet Client is not installed (Control Panel > Programs > "
                               "Turn Windows features on or off > Telnet Client)")
        return ConsoleCommand(f"{label} (telnet)", [exe, host], {})
    return ConsoleCommand(f"{label} (telnet)", ["telnet", host], {})


def http_url(ip: str, https: bool) -> str:
    host = validate.host(ip)
    if ":" in host:
        host = f"[{host}]"
    return f"{'https' if https else 'http'}://{host}/"


def open_url(url: str) -> None:
    if plat.IS_WINDOWS:
        os.startfile(url)                          # default browser  # noqa: S606
        return
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ------------------------------------------------------------------- D-Bus
def _qdbus_bin() -> str | None:
    for b in ("qdbus6", "qdbus", "qdbus-qt6", "qdbus-qt5"):
        p = shutil.which(b)
        if p:
            return p
    return None


def gvariant_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_dbus_call(service: str, path: str, iface: str, method: str, args: list,
                    tool: str | None = None) -> list[str]:
    """argv for one D-Bus method call (qdbus6 preferred, gdbus fallback)."""
    tool = tool if tool is not None else (_qdbus_bin() or "gdbus")
    if os.path.basename(tool).startswith("qdbus"):
        return [tool, service, path, f"{iface}.{method}", *[str(a) for a in args]]
    out = ["gdbus", "call", "--session", "--dest", service, "--object-path", path,
           "--method", f"{iface}.{method}"]
    for a in args:
        out.append(str(a) if isinstance(a, int) and not isinstance(a, bool) else gvariant_str(str(a)))
    return out


def dbus_call(service, path, iface, method, *args, timeout=5.0) -> str:
    cmd = build_dbus_call(service, path, iface, method, list(args))
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"D-Bus {method} failed: {(cp.stderr or cp.stdout).strip()}")
    out = cp.stdout.strip()
    if out.startswith("(") and out.endswith(",)"):          # gdbus tuple "(3,)"
        out = out[1:-2].strip().strip("'")
    return out


def list_konsole_services() -> list[str]:
    q = _qdbus_bin()
    if q:
        cp = subprocess.run([q], capture_output=True, text=True, timeout=5)
        names = cp.stdout.split()
    else:
        cp = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                             "--object-path", "/org/freedesktop/DBus", "--method",
                             "org.freedesktop.DBus.ListNames"], capture_output=True, text=True, timeout=5)
        names = [n.strip(" '[](),") for n in cp.stdout.split(",")]
    return [n for n in names if n.startswith("org.kde.konsole-")]


def profile_text(python: str = "/usr/bin/python3") -> str:
    return ("[General]\n"
            f"Name={PROFILE_NAME}\n"
            f"Command={python} -I -S {TABEXEC}\n"
            "Parent=FALLBACK/\n"
            "LocalTabTitleFormat=%w\n"
            "RemoteTabTitleFormat=%w\n"
            "\n[Scrolling]\nHistoryMode=2\n"
            "\n[Terminal Features]\nUrlHintsModifiers=0\n"
            "\n[Keyboard]\nKeyBindings=default\n")


def ensure_profile() -> Path:
    d = Path(os.path.expanduser("~/.local/share/konsole"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{PROFILE_NAME}.profile"
    text = profile_text()
    if not p.exists() or p.read_text() != text:
        p.write_text(text)
    return p


def _pid_is_labdesk_konsole(pid: int) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return bool(cmd) and cmd[0].endswith(b"konsole") and b"--separate" in cmd and PROFILE_NAME.encode() in cmd


class KonsoleLauncher:
    """One dedicated Konsole window; one tab per device."""

    IFACE_WIN = "org.kde.konsole.Window"
    IFACE_SES = "org.kde.konsole.Session"

    def __init__(self):
        self.service: str | None = None
        self.proc: subprocess.Popen | None = None

    def _find_existing(self) -> str | None:
        if self.service:
            pid = int(self.service.rsplit("-", 1)[1])
            if _pid_is_labdesk_konsole(pid):
                return self.service
        for svc in list_konsole_services():
            try:
                pid = int(svc.rsplit("-", 1)[1])
            except ValueError:
                continue
            if _pid_is_labdesk_konsole(pid):
                return svc
        return None

    def _wait_service(self, svc: str, timeout=8.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if svc in list_konsole_services():
                try:
                    dbus_call(svc, "/Windows/1", self.IFACE_WIN, "sessionCount")
                    return True
                except Exception:
                    pass
            time.sleep(0.15)
        return False

    def open(self, cmd: ConsoleCommand) -> str:
        """Open a tab running ``cmd``. Returns a description of what was done."""
        if not shutil.which("konsole"):
            return self._fallback(cmd)
        ensure_profile()
        svc = self._find_existing()
        if svc is None:
            env = dict(os.environ)
            self.proc = subprocess.Popen(
                ["konsole", "--separate", "--profile", PROFILE_NAME, "--hide-menubar",
                 "--title", "LabDesk consoles"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            svc = f"org.kde.konsole-{self.proc.pid}"
            if not self._wait_service(svc):
                return self._fallback(cmd)
            sid = int(dbus_call(svc, "/Windows/1", self.IFACE_WIN, "currentSession"))
        else:
            sid = int(dbus_call(svc, "/Windows/1", self.IFACE_WIN, "newSession", PROFILE_NAME))
        self.service = svc
        spath = f"/Sessions/{sid}"
        for ctx in (0, 1):
            dbus_call(svc, spath, self.IFACE_SES, "setTabTitleFormat", ctx, cmd.title)
        dbus_call(svc, spath, self.IFACE_SES, "runCommand", cmd.as_json())
        try:
            dbus_call(svc, "/Windows/1", self.IFACE_WIN, "setCurrentSession", sid)
        except Exception:
            pass
        return f"Konsole tab '{cmd.title}' (session {sid} in {svc})"

    def sessions(self) -> list[dict]:
        svc = self._find_existing()
        if not svc:
            return []
        out = []
        for sid in dbus_call(svc, "/Windows/1", self.IFACE_WIN, "sessionList").split():
            sid = sid.strip("[],'\"")
            if not sid.isdigit():
                continue
            title = dbus_call(svc, f"/Sessions/{sid}", self.IFACE_SES, "title", 1)
            out.append({"id": int(sid), "title": title})
        return out

    def _fallback(self, cmd: ConsoleCommand) -> str:
        """No D-Bus: new Konsole window (or xterm) running the command directly."""
        envp = [f"{k}={v}" for k, v in cmd.env.items()]
        if shutil.which("konsole"):
            argv = ["konsole", "--separate", "-p", f"tabtitle={cmd.title}", "-e", "env", *envp, *cmd.argv]
        elif shutil.which("xterm"):
            argv = ["xterm", "-T", cmd.title, "-e", "env", *envp, *cmd.argv]
        else:
            raise RuntimeError("neither konsole nor xterm found")
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return f"new terminal window '{cmd.title}'"


# ------------------------------------------------------------------- Windows
_WT_WINDOW = "LabDesk"


def _win_title(title: str) -> str:
    """A tab / window title that is safe on a cmd.exe / wt.exe command line."""
    import re
    return re.sub(r"[^A-Za-z0-9 ._()\-]", "_", title)[:60] or "LabDesk"


def wt_argv(wt: str, cmd: ConsoleCommand) -> list[str]:
    """``wt -w LabDesk new-tab --title T --suppressApplicationTitle <argv>``: a tab in the one window
    named LabDesk (created on first use).  wt splits its command line on ';' - escape it."""
    return [wt, "-w", _WT_WINDOW, "new-tab", "--title", _win_title(cmd.title), "--suppressApplicationTitle",
            *[a.replace(";", "\\;") for a in cmd.argv]]


def conhost_cmdline(cmd: ConsoleCommand) -> str:
    """cmd.exe line for a NEW console window: set the title, run the command, keep the window open
    (pause) when it fails so the error can be read."""
    inner = subprocess.list2cmdline(cmd.argv)
    return f'cmd.exe /d /s /c "title {_win_title(cmd.title)} & {inner} || pause"'


class WindowsConsoleLauncher:
    """Windows: Windows Terminal tab per device (real console: Tab, ?, Ctrl-C, arrows); otherwise a new
    console window.  ``popen`` is injectable for tests."""

    def __init__(self, popen=None, wt_finder=None):
        self._popen = popen or subprocess.Popen
        self._wt_finder = wt_finder or plat.windows_terminal_exe

    def open(self, cmd: ConsoleCommand) -> str:
        env = dict(os.environ)
        env.update(cmd.env)
        wt = self._wt_finder()
        if wt:
            try:
                self._popen(wt_argv(wt, cmd), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **plat.no_window_kwargs())
                return f"Windows Terminal tab '{cmd.title}'"
            except OSError:
                pass
        self._popen(conhost_cmdline(cmd), env=env, creationflags=plat.CREATE_NEW_CONSOLE)
        return f"new console window '{cmd.title}'"

    def sessions(self) -> list[dict]:
        return []


def default_launcher():
    """The console launcher for this OS."""
    return WindowsConsoleLauncher() if plat.IS_WINDOWS else KonsoleLauncher()
