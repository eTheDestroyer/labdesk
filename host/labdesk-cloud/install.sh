#!/bin/bash
# Install / update the LabDesk simulated Internet cloud on this host (run as root):
#     sudo host/labdesk-cloud/install.sh [owner]        (owner of /var/lib/labdesk-cloud, default $SUDO_USER)
# Installs:
#   /etc/labdesk-cloud.conf                         (kept if it already exists)
#   /usr/local/libexec/labdesk-cloud/{common.sh,up,down,check}
#   /etc/systemd/system/labdesk-cloud.service, labdesk-cloud-check.{service,timer}, labdesk-cloud-routes.path
#   /var/lib/labdesk-cloud/                         (owned by <owner>: LabDesk writes routes.conf there)
#   a block in /etc/libvirt/hooks/network           (re-attach when the libvirt 'default' network restarts)
# then enables + (re)starts labdesk-cloud.service.  Idempotent.  Remove with uninstall.sh.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
OWNER=${1:-${SUDO_USER:-}}
[ -n "$OWNER" ] && id "$OWNER" >/dev/null 2>&1 || { echo "usage: $0 <owner-user>" >&2; exit 1; }
[[ $OWNER =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "bad user name" >&2; exit 1; }

install -d -m 0755 /usr/local/libexec/labdesk-cloud
install -m 0644 "$HERE/libexec/common.sh" /usr/local/libexec/labdesk-cloud/common.sh
for f in up down check; do
    install -m 0755 "$HERE/libexec/$f" "/usr/local/libexec/labdesk-cloud/$f"
done
if [ ! -e /etc/labdesk-cloud.conf ]; then
    sed "s/@OWNER@/$OWNER/" "$HERE/labdesk-cloud.conf" > /etc/labdesk-cloud.conf
    chmod 0644 /etc/labdesk-cloud.conf
fi
for u in labdesk-cloud.service labdesk-cloud-check.service labdesk-cloud-check.timer labdesk-cloud-routes.path; do
    install -m 0644 "$HERE/systemd/$u" "/etc/systemd/system/$u"
done
install -d -m 0755 -o "$OWNER" -g "$(id -gn "$OWNER")" /var/lib/labdesk-cloud

HOOK=/etc/libvirt/hooks/network
if [ -d /etc/libvirt/hooks ] && ! grep -q 'labdesk-cloud' "$HOOK" 2>/dev/null; then
    if [ ! -e "$HOOK" ]; then
        printf '#!/bin/sh\n# Called by libvirt as: network <name> <operation> <sub-operation>\n[ "$1" = "default" ] || exit 0\nexit 0\n' > "$HOOK"
        chmod 0755 "$HOOK"
    fi
    cp -p "$HOOK" "$HOOK.pre-labdesk-cloud"
    BLOCK=$(mktemp)
    cat > "$BLOCK" <<'HOOKBLOCK'
# >>> labdesk-cloud: re-attach the simulated Internet cloud when the default network (re)starts
if [ "$2" = "started" ] && [ -e /run/labdesk-cloud/active ] && [ -x /usr/local/libexec/labdesk-cloud/up ]; then
    /usr/local/libexec/labdesk-cloud/up --quiet >/dev/null 2>&1 || true
fi
# <<< labdesk-cloud
HOOKBLOCK
    last=$(grep -nx 'exit 0' "$HOOK" | tail -n1 | cut -d: -f1 || true)
    if [ -n "$last" ]; then          # insert before the LAST 'exit 0' (existing behaviour unchanged)
        { head -n $((last - 1)) "$HOOK"; cat "$BLOCK"; tail -n +"$last" "$HOOK"; } > "$HOOK.tmp"
    else
        { cat "$HOOK"; cat "$BLOCK"; } > "$HOOK.tmp"
    fi
    rm -f "$BLOCK"
    chmod 0755 "$HOOK.tmp"
    mv -f "$HOOK.tmp" "$HOOK"
    echo "extended $HOOK (backup: $HOOK.pre-labdesk-cloud)"
fi

systemctl daemon-reload
systemctl enable labdesk-cloud.service >/dev/null
systemctl restart labdesk-cloud.service
systemctl start labdesk-cloud-check.timer labdesk-cloud-routes.path
echo "labdesk-cloud installed and running: $(systemctl is-active labdesk-cloud.service)"
