#!/usr/bin/env bash
# LabDesk installer: run it from the folder you cloned.
#   ./install.sh                 first install (asks for your CML login once)
#   ./install.sh --reconfigure   ask for the CML address / login again
#   ./install.sh --uninstall     remove the menu entry and the virtualenv (keeps your files)
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CRED_FILE="${CML_CRED_FILE:-$HOME/cml-labs/cml-credentials.env}"
DESKTOP_FILE="$HOME/.local/share/applications/labdesk.desktop"
MODE="${1:-install}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m ✗ %s\033[0m\n' "$*" >&2; exit 1; }

if [[ "$MODE" == "--uninstall" ]]; then
    rm -f "$DESKTOP_FILE" && ok "menu entry removed"
    rm -rf "$HERE/.venv" && ok "virtualenv removed"
    echo "Your credentials file ($CRED_FILE) and settings (~/.config/labdesk, ~/.config/LabDesk) were kept."
    exit 0
fi

# 1. system requirements -------------------------------------------------------------
say "Checking requirements"
missing=()
command -v python3 >/dev/null || missing+=(python3)
python3 -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3-venv)
command -v ssh     >/dev/null || missing+=(openssh-client)
command -v konsole >/dev/null || missing+=(konsole)
command -v virsh   >/dev/null || missing+=(libvirt-clients)
if command -v ldconfig >/dev/null && ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor.so.0; then
    missing+=(libxcb-cursor0)
fi
if ((${#missing[@]})); then
    die "Missing packages. Install them, then run ./install.sh again:
    sudo apt install ${missing[*]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11 or newer is needed (you have $(python3 --version))."
ok "python3 $(python3 -c 'import platform; print(platform.python_version())'), ssh, konsole, virsh"

# 2. virtualenv + Python packages ----------------------------------------------------
say "Installing Python packages into $HERE/.venv (first time takes a minute or two)"
[[ -x "$HERE/.venv/bin/python" ]] || python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/python" -m pip install --quiet --upgrade pip
"$HERE/.venv/bin/python" -m pip install --quiet -r "$HERE/requirements.txt"
ok "PySide6, pexpect, PyYAML, websocket-client installed"

# 3. CML address + login (stored once, mode 0600, outside the repo) ------------------
if [[ ! -s "$CRED_FILE" || "$MODE" == "--reconfigure" ]]; then
    say "CML controller login (saved to $CRED_FILE, readable only by you)"
    read -r -p "  CML address [192.168.122.60]: " host;  host="${host:-192.168.122.60}"
    read -r -p "  CML username [admin]: " user;          user="${user:-admin}"
    read -r -s -p "  CML password: " pass; echo
    [[ -n "$pass" ]] || die "The password can't be empty."
    mkdir -p "$(dirname "$CRED_FILE")"
    old=""
    [[ -f "$CRED_FILE" ]] && old="$(grep -vE '^(CML_HOST|CML_ADMIN_USER|CML_ADMIN_PASS)=' "$CRED_FILE" || true)"
    umask 077
    { printf 'CML_HOST=%s\nCML_ADMIN_USER=%s\nCML_ADMIN_PASS=%s\n' "$host" "$user" "$pass"
      [[ -n "$old" ]] && printf '%s\n' "$old"; } > "$CRED_FILE.tmp"
    mv "$CRED_FILE.tmp" "$CRED_FILE"
    chmod 600 "$CRED_FILE"
    unset pass
    ok "credentials saved"
else
    chmod 600 "$CRED_FILE"
    ok "using existing credentials in $CRED_FILE (./install.sh --reconfigure to change)"
fi

# 4. application-menu entry -----------------------------------------------------------
say "Adding LabDesk to the application menu"
chmod +x "$HERE/labdesk"
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=LabDesk
GenericName=Network Lab Desktop
Comment=Build and run Cisco CML labs; real serial consoles in LabDesk's own tabbed console window
Exec=$HERE/labdesk
Icon=$HERE/labdesk.svg
Terminal=false
Categories=Network;
Keywords=CML;Cisco;lab;network;console;
StartupWMClass=labdesk
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$DESKTOP_FILE")" || true
ok "menu entry: $DESKTOP_FILE"

# 5. quick check that CML answers --------------------------------------------------
say "Checking that the CML controller answers"
if (cd "$HERE" && "$HERE/.venv/bin/python" - <<'PY'
import ssl, sys, urllib.request
sys.path.insert(0, ".")
from labdesk_app import config
ctx = ssl._create_unverified_context()
try:
    urllib.request.urlopen(f"https://{config.CML_HOST}/api/v0/system_information", context=ctx, timeout=5)
    print(f"   CML at {config.CML_HOST} answers")
except Exception as e:
    print(f"   CML at {config.CML_HOST} did not answer ({e.__class__.__name__}); start the CML VM (virsh start cml) and try LabDesk again")
    sys.exit(1)
PY
); then ok "CML reachable"; else warn "CML not reachable right now - that's OK, LabDesk can start it for you"; fi

echo
ok "Done. Open LabDesk from the application menu, or run:  $HERE/labdesk"
