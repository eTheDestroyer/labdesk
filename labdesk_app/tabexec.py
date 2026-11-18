#!/usr/bin/python3 -I -S
"""Konsole tab bootstrap for LabDesk (the LabDesk profile's Command).

Reads ONE JSON line from the tty with echo disabled: {"title", "argv", "env"}, then runs
argv on this tab's pty (real tty: Tab, ?, Ctrl-keys, arrows are passed through untouched).
Only whitelisted programs are allowed. If the program fails, the tab stays open with the
error visible until Enter is pressed.
"""
import json
import os
import signal
import subprocess
import sys
import termios

ALLOWED = {"ssh", "virsh", "telnet"}


def read_request():
    fd = sys.stdin.fileno()
    old = None
    try:
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, new)
    except termios.error:
        pass
    try:
        line = sys.stdin.readline()
    finally:
        if old is not None:
            termios.tcsetattr(fd, termios.TCSANOW, old)
    return json.loads(line)


def main():
    try:
        req = read_request()
        argv = [str(a) for a in req["argv"]]
        env = {str(k): str(v) for k, v in (req.get("env") or {}).items()}
    except Exception as e:  # noqa: BLE001
        print(f"LabDesk: bad request ({e})")
        input("Press Enter to close")
        return 2
    if not argv or os.path.basename(argv[0]) not in ALLOWED:
        print("LabDesk: refusing to run", argv[:1])
        input("Press Enter to close")
        return 2
    title = req.get("title", "")
    sys.stdout.write("\033]0;%s\007\033[H\033[2J" % title.replace("\007", ""))
    hint = "Ctrl-] to leave the console" if argv[0] in ("ssh", "virsh") else ""
    sys.stdout.write("\033[2m[LabDesk] %s  %s\033[0m\r\n" % (title, hint))
    sys.stdout.flush()
    full_env = dict(os.environ)
    full_env.update(env)
    for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(s, signal.SIG_IGN)
    try:
        rc = subprocess.call(argv, env=full_env,
                             preexec_fn=lambda: [signal.signal(s, signal.SIG_DFL)
                                                 for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP)])
    except FileNotFoundError:
        rc = 127
        print(f"LabDesk: {argv[0]} not found")
    if rc != 0:
        sys.stdout.write(f"\r\n[LabDesk] {argv[0]} exited with status {rc}. Press Enter to close this tab.")
        sys.stdout.flush()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
