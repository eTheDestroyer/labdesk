# shellcheck shell=bash
# Shared helpers for /usr/local/libexec/labdesk-cloud/{up,down,check}.  Root only.
#
# Config files are PARSED, never sourced:
#   /etc/labdesk-cloud.conf              root-owned defaults (NS, BRIDGE, CLOUD_ADDR, ANSWER_IPS, ...)
#   /var/lib/labdesk-cloud/routes.conf   user-writable (LabDesk): EDGE_GW=<ip> and ROUTE <prefix> <gw>
# Everything from the user-writable file is validated strictly and can only ever change routes
# INSIDE the cloud namespace (never the host's routing).

set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 022

CONF=${LABDESK_CLOUD_CONF:-/etc/labdesk-cloud.conf}
STATE_DIR=/var/lib/labdesk-cloud
USER_ROUTES=${LABDESK_CLOUD_USER_ROUTES:-$STATE_DIR/routes.conf}
RUN_DIR=/run/labdesk-cloud
STATUS=$RUN_DIR/status
ACTIVE=$RUN_DIR/active
RT_PROTO=77                 # routes owned by labdesk-cloud inside the namespace ('proto 77')
VETH_NS=cloud0              # interface name inside the namespace

log() { echo "labdesk-cloud: $*" >&2; }
die() { log "$*"; exit 1; }

# ------------------------------------------------------------------ parsing / validation
conf_get() {   # conf_get KEY DEFAULT  -> value of KEY= in $CONF (quotes stripped), or DEFAULT
    local v=""
    if [ -r "$CONF" ]; then
        v=$(grep -E "^[[:space:]]*$1=" "$CONF" | tail -n1 | sed -E "s/^[[:space:]]*$1=//; s/^[\"']//; s/[\"'][[:space:]]*$//; s/[[:space:]]+#.*$//")
    fi
    [ -n "$v" ] && printf '%s' "$v" || printf '%s' "$2"
}

is_ipv4() {
    local ip=$1 o
    [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do
        [ $((10#$o)) -le 255 ] || return 1
    done
}

ip2int() {
    local IFS=.
    # shellcheck disable=SC2086
    set -- $1
    echo $(( (10#$1 << 24) | (10#$2 << 16) | (10#$3 << 8) | 10#$4 ))
}

is_prefix() {   # a.b.c.d/len with no host bits set
    local p=$1 ip len
    [[ $p =~ ^([0-9.]+)/([0-9]{1,2})$ ]] || return 1
    ip=${BASH_REMATCH[1]}; len=$((10#${BASH_REMATCH[2]}))
    is_ipv4 "$ip" && [ "$len" -ge 8 ] && [ "$len" -le 32 ] || return 1
    local mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
    [ $(( $(ip2int "$ip") & ~mask & 0xFFFFFFFF )) -eq 0 ]
}

in_prefix() {   # in_prefix IP PREFIX
    local ip=$1 net=${2%/*} len=${2#*/}
    local mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
    [ $(( $(ip2int "$ip") & mask )) -eq $(( $(ip2int "$net") & mask )) ]
}

prefix_inside() {   # prefix_inside CHILD PARENT  (CHILD is PARENT or more specific)
    local c=$1 p=$2
    [ "${c#*/}" -ge "${p#*/}" ] && in_prefix "${c%/*}" "$p"
}

private_prefix() {   # only lab-style space may be routed to a router
    local p=$1 r
    for r in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10; do
        prefix_inside "$p" "$r" && return 0
    done
    return 1
}

load_config() {
    NS=$(conf_get NS labcloud)
    BRIDGE=$(conf_get BRIDGE virbr0)
    CLOUD_ADDR=$(conf_get CLOUD_ADDR 100.64.0.1/24)
    ANSWER_IPS=$(conf_get ANSWER_IPS "8.8.8.8 1.1.1.1")
    RETURN_PREFIXES=$(conf_get RETURN_PREFIXES "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16")
    CONF_EDGE_GW=$(conf_get EDGE_GW "")
    STATE_OWNER=$(conf_get STATE_OWNER "")
    HOST_ROUTE=$(conf_get HOST_ROUTE yes)
    CLOUD_MAC=$(conf_get CLOUD_MAC 02:6c:64:63:6c:01)
    [[ $CLOUD_MAC =~ ^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$ ]] || die "bad CLOUD_MAC '$CLOUD_MAC'"
    [[ $NS =~ ^[A-Za-z0-9_-]{1,12}$ ]] || die "bad NS '$NS'"
    [[ $BRIDGE =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || die "bad BRIDGE '$BRIDGE'"
    [[ $CLOUD_ADDR =~ ^([0-9.]+)/([0-9]{1,2})$ ]] || die "bad CLOUD_ADDR '$CLOUD_ADDR'"
    CLOUD_IP=${BASH_REMATCH[1]}
    CLOUD_LEN=$((10#${BASH_REMATCH[2]}))
    is_ipv4 "$CLOUD_IP" && [ "$CLOUD_LEN" -ge 8 ] && [ "$CLOUD_LEN" -le 30 ] || die "bad CLOUD_ADDR '$CLOUD_ADDR'"
    local mask=$(( (0xFFFFFFFF << (32 - CLOUD_LEN)) & 0xFFFFFFFF )) n
    n=$(( $(ip2int "$CLOUD_IP") & mask ))
    CLOUD_NET="$(( n >> 24 & 255 )).$(( n >> 16 & 255 )).$(( n >> 8 & 255 )).$(( n & 255 ))/$CLOUD_LEN"
    local a
    for a in $ANSWER_IPS; do is_ipv4 "$a" || die "bad ANSWER_IPS entry '$a'"; done
    for a in $RETURN_PREFIXES; do is_prefix "$a" || die "bad RETURN_PREFIXES entry '$a'"; done
    VETH_HOST="${NS:0:12}-br"
}

valid_gw() {   # a router address inside the cloud subnet (not the cloud itself / net / broadcast)
    local g=$1
    is_ipv4 "$g" && in_prefix "$g" "$CLOUD_NET" && [ "$g" != "$CLOUD_IP" ] || return 1
    local host=$(( $(ip2int "$g") & ((1 << (32 - CLOUD_LEN)) - 1) ))
    [ "$host" -ne 0 ] && [ "$host" -ne $(( (1 << (32 - CLOUD_LEN)) - 1 )) ]
}

# desired_routes -> lines "prefix gw" (EDGE_GW for RETURN_PREFIXES + validated ROUTE lines)
desired_routes() {
    local edge=$CONF_EDGE_GW line key val pfx gw n=0
    declare -A per=()
    if [ -f "$USER_ROUTES" ] && [ ! -L "$USER_ROUTES" ] && [ "$(stat -c %s "$USER_ROUTES")" -le 16384 ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=${line%%#*}
            line=$(echo "$line" | tr -s '[:space:]' ' ' | sed -E 's/^ //; s/ $//')
            [ -z "$line" ] && continue
            if [[ $line =~ ^EDGE_GW=\ ?([0-9.]*)$ ]]; then
                val=${BASH_REMATCH[1]}
                if [ -z "$val" ]; then edge=""
                elif valid_gw "$val"; then edge=$val
                else log "ignoring EDGE_GW=$val (not a router address in $CLOUD_NET)"; fi
            elif [[ $line =~ ^ROUTE\ ([0-9./]+)\ ([0-9.]+)$ ]]; then
                pfx=${BASH_REMATCH[1]}; gw=${BASH_REMATCH[2]}
                if [ $n -ge 64 ]; then log "more than 64 ROUTE lines - ignoring the rest"; break; fi
                if is_prefix "$pfx" && private_prefix "$pfx" && ! prefix_inside "$pfx" "$CLOUD_NET" && valid_gw "$gw"; then
                    per[$pfx]=$gw; n=$((n + 1))
                else
                    log "ignoring 'ROUTE $pfx $gw' (prefix must be private lab space, gateway a router in $CLOUD_NET)"
                fi
            else
                log "ignoring unknown line in $USER_ROUTES: $line"
            fi
        done < "$USER_ROUTES"
    elif [ -e "$USER_ROUTES" ]; then
        log "ignoring $USER_ROUTES (not a regular file or larger than 16 KiB)"
    fi
    if [ -n "$edge" ]; then
        valid_gw "$edge" || { log "ignoring EDGE_GW=$edge"; edge=""; }
    fi
    if [ -n "$edge" ]; then
        for pfx in $RETURN_PREFIXES; do
            [ -n "${per[$pfx]:-}" ] || echo "$pfx $edge"
        done
    fi
    for pfx in "${!per[@]}"; do echo "$pfx ${per[$pfx]}"; done
    EDGE_EFFECTIVE=$edge
}

apply_routes() {
    local want have pfx gw
    want=$(desired_routes | sort -u)
    have=$(ip -n "$NS" -4 route show proto $RT_PROTO 2>/dev/null \
           | sed -nE 's/^([0-9./]+) via ([0-9.]+) .*/\1 \2/p' | sort -u)
    if [ "$want" != "$have" ]; then
        ip -n "$NS" -4 route flush proto $RT_PROTO 2>/dev/null || true
        while read -r pfx gw; do
            [ -n "$pfx" ] || continue
            ip -n "$NS" route replace "$pfx" via "$gw" dev "$VETH_NS" proto $RT_PROTO onlink \
                || log "route $pfx via $gw failed"
        done <<< "$want"
        log "return routes: $(echo "$want" | tr '\n' ';')"
    fi
}

ns_exists() { [ -e "/run/netns/$NS" ]; }

bridge_src() {   # first IPv4 address of the bridge (the host's source for the health ping)
    ip -4 -o addr show dev "$BRIDGE" 2>/dev/null | awk '{print $4}' | head -n1 | cut -d/ -f1
}

write_status() {   # write_status key=value ...   (atomic, world-readable)
    mkdir -p "$RUN_DIR"
    local tmp
    tmp=$(mktemp "$RUN_DIR/.status.XXXXXX")
    {
        echo "# labdesk-cloud health - written by /usr/local/libexec/labdesk-cloud/check"
        echo "ts=$(date +%s)"
        printf '%s\n' "$@"
    } > "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$STATUS"
}
