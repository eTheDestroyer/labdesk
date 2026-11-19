"""Console / SSH / Telnet / HTTP launchers.

Console commands are argv lists that run on a REAL pty - on Linux inside the LabDesk console window
(labdesk_app/ui/console, installed by MainWindow; this module stays free of Qt), else inside Konsole:

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
    meta: dict = field(default_factory=dict)      # for the console window only (key, kind, node_definition,
                                                  # lab_title); never part of as_json()

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
    if plat.IS_WINDOWS:
        # the LabDesk console window runs it in process (paramiko, saved login: nothing to type); Windows
        # Terminal (fallback) runs the ssh argv and ssh asks for the CML password
        return ConsoleCommand(validate.cml_label(label), argv, {},
                              {"transport": {"type": "cml", "uuid": validate.uuid(console_uuid),
                                             "user": validate.username(user)}})
    return ConsoleCommand(validate.cml_label(label), argv, askpass_env())


def kvm_console(vm: str) -> ConsoleCommand:
    if plat.IS_WINDOWS:
        from .vmware import pipe_name
        pipe = pipe_name(vm)
        # VMware VM: its serial port is the named pipe \\.\pipe\labdesk-<vm> (LabDesk console window)
        return ConsoleCommand(vm, ["vmware-serial", pipe], {}, {"transport": {"type": "pipe", "path": pipe}})
    from .kvm import build_console_cmd
    return ConsoleCommand(vm, build_console_cmd(vm), {})


LEGACY_SSH_OPTS = ["-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1",
                   "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]


def ssh_to_device(label: str, ip: str, user: str = "admin") -> ConsoleCommand:
    argv = [plat.ssh_exe(), *_known_hosts_opt(config.DEVICE_KNOWN_HOSTS),
            "-o", "StrictHostKeyChecking=no", "-o", "PubkeyAuthentication=no",
            *LEGACY_SSH_OPTS, "-l", validate.username(user), validate.host(ip)]
    return ConsoleCommand(f"{label} (ssh)", argv, {}, {"kind": "ssh"})


def telnet_to_device(label: str, ip: str) -> ConsoleCommand:
    host = validate.host(ip)
    if plat.IS_WINDOWS:
        exe = shutil.which("telnet")
        if not exe:
            raise RuntimeError("the Windows Telnet Client is not installed (Control Panel > Programs > "
                               "Turn Windows features on or off > Telnet Client)")
        return ConsoleCommand(f"{label} (telnet)", [exe, host], {}, {"kind": "telnet"})
    return ConsoleCommand(f"{label} (telnet)", ["telnet", host], {}, {"kind": "telnet"})


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


# The LabDesk console look: neon green on black.  It is a second Konsole profile (LabDeskNeon) next to the
# plain one, so switching the look also works for the next tab of a console window that is already open
# (Konsole caches a profile it has loaded; a profile with another name is read from disk).
NEON_PROFILE_NAME = "LabDeskNeon"
NEON_SCHEME_NAME = "LabDeskNeon"
NEON_GREEN = (57, 255, 20)             # #39FF14
NEON_COLOURS = {                       # ANSI 0-7: (normal, intense).  Errors stay red, the rest glows green-ish
    0: ((6, 12, 6), (40, 70, 40)),
    1: ((255, 49, 49), (255, 110, 110)),
    2: (NEON_GREEN, (140, 255, 120)),
    3: ((204, 255, 0), (230, 255, 100)),
    4: ((0, 200, 255), (110, 225, 255)),
    5: ((255, 60, 200), (255, 130, 225)),
    6: ((0, 255, 200), (130, 255, 225)),
    7: ((170, 255, 150), (225, 255, 215)),
}
NEON_BACKGROUND = (3, 8, 3)


def neon_colorscheme_text() -> str:
    """Konsole colour scheme 'LabDeskNeon': neon green text on near-black."""
    def rgb(c):
        return ",".join(str(v) for v in c)

    def faint(c):
        return tuple(int(v * 0.55) for v in c)
    out = [f"[Background]\nColor={rgb(NEON_BACKGROUND)}\n",
           f"[BackgroundFaint]\nColor={rgb(NEON_BACKGROUND)}\n",
           f"[BackgroundIntense]\nColor={rgb(NEON_BACKGROUND)}\n"]
    for i, (normal, intense) in NEON_COLOURS.items():
        out += [f"[Color{i}]\nColor={rgb(normal)}\n", f"[Color{i}Faint]\nColor={rgb(faint(normal))}\n",
                f"[Color{i}Intense]\nColor={rgb(intense)}\n"]
    out += [f"[Foreground]\nColor={rgb(NEON_GREEN)}\n", f"[ForegroundFaint]\nColor={rgb(faint(NEON_GREEN))}\n",
            f"[ForegroundIntense]\nBold=true\nColor={rgb(NEON_COLOURS[2][1])}\n",
            "[General]\nAnchor=0.5,0.5\nBlur=false\nColorRandomization=false\nDescription=LabDesk Neon\n"
            "FillStyle=Tile\nOpacity=1\nWallpaper=\n"]
    return "\n".join(out)


def neon_profile_text(python: str = "/usr/bin/python3") -> str:
    """The plain LabDesk profile plus the neon colour scheme and a solid green block cursor."""
    base = profile_text(python).replace(f"Name={PROFILE_NAME}\n", f"Name={NEON_PROFILE_NAME}\n", 1)
    return (base + f"\n[Appearance]\nColorScheme={NEON_SCHEME_NAME}\nBoldIntense=true\n"
            f"\n[Cursor Options]\nCursorShape=0\nUseCustomCursorColor=true\n"
            f"CustomCursorColor={','.join(str(v) for v in NEON_GREEN)}\nCustomCursorTextColor=0,0,0\n")


def _write_if_changed(p: Path, text: str) -> None:
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


def ensure_profile(neon: bool = False, directory: Path | None = None) -> Path:
    """Write the Konsole profile LabDesk tabs use (and, for the neon look, its colour scheme).  Returns the
    profile file; its stem is the profile name."""
    d = Path(directory) if directory else Path(os.path.expanduser("~/.local/share/konsole"))
    d.mkdir(parents=True, exist_ok=True)
    _write_if_changed(d / f"{PROFILE_NAME}.profile", profile_text())
    if not neon:
        return d / f"{PROFILE_NAME}.profile"
    scheme = d / f"{NEON_SCHEME_NAME}.colorscheme"
    if not scheme.exists():              # created once: the user may tune the colours (delete it to reset)
        scheme.write_text(neon_colorscheme_text(), encoding="utf-8")
    _write_if_changed(d / f"{NEON_PROFILE_NAME}.profile", neon_profile_text())
    return d / f"{NEON_PROFILE_NAME}.profile"


def _pid_is_labdesk_konsole(pid: int) -> bool:
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return bool(cmd) and cmd[0].endswith(b"konsole") and b"--separate" in cmd and \
        (PROFILE_NAME.encode() in cmd or NEON_PROFILE_NAME.encode() in cmd)


class KonsoleLauncher:
    """One dedicated Konsole window; one tab per device."""

    IFACE_WIN = "org.kde.konsole.Window"
    IFACE_SES = "org.kde.konsole.Session"

    def __init__(self, neon: bool = True):
        self.service: str | None = None
        self.proc: subprocess.Popen | None = None
        self.neon = neon                 # neon green on black (View > Neon green console)

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
        profile = ensure_profile(self.neon).stem
        svc = self._find_existing()
        if svc is None:
            env = dict(os.environ)
            self.proc = subprocess.Popen(
                ["konsole", "--separate", "--profile", profile, "--hide-menubar",
                 "--title", "LabDesk consoles"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
            svc = f"org.kde.konsole-{self.proc.pid}"
            if not self._wait_service(svc):
                return self._fallback(cmd)
            sid = int(dbus_call(svc, "/Windows/1", self.IFACE_WIN, "currentSession"))
        else:
            sid = int(dbus_call(svc, "/Windows/1", self.IFACE_WIN, "newSession", profile))
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
