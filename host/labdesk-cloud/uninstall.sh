#!/bin/bash
# Remove the LabDesk simulated Internet cloud from this host (run as root).  The libvirt hook block
# between '# >>> labdesk-cloud' and '# <<< labdesk-cloud' is removed; the rest of the hook stays.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }
systemctl disable --now labdesk-cloud.service 2>/dev/null || true
systemctl stop labdesk-cloud-check.timer labdesk-cloud-routes.path 2>/dev/null || true
rm -f /etc/systemd/system/labdesk-cloud.service /etc/systemd/system/labdesk-cloud-check.service \
      /etc/systemd/system/labdesk-cloud-check.timer /etc/systemd/system/labdesk-cloud-routes.path
systemctl daemon-reload
HOOK=/etc/libvirt/hooks/network
if [ -f "$HOOK" ] && grep -q '# >>> labdesk-cloud' "$HOOK"; then
    sed -i '/^# >>> labdesk-cloud/,/^# <<< labdesk-cloud/d' "$HOOK"
fi
rm -rf /usr/local/libexec/labdesk-cloud /run/labdesk-cloud /var/lib/labdesk-cloud
rm -f /etc/labdesk-cloud.conf "$HOOK.pre-labdesk-cloud"
echo "labdesk-cloud removed"
