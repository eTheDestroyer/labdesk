"""Name validation for CML node labels, lab titles and libvirt object names."""
from __future__ import annotations

import ipaddress
import re

# CML accepts almost anything, but labels end up in console paths (/lab/node/0), file
# names and terminal titles - keep them shell/console friendly.
_CML_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,62}$")
# libvirt domain/network names: no '/', keep it conservative (also used as volume names)
_VIRSH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,47}$")
_LAB_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()]{0,63}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,31}$")


class ValidationError(ValueError):
    pass


def cml_label(label: str) -> str:
    label = (label or "").strip()
    if not _CML_LABEL.fullmatch(label):
        raise ValidationError(
            f"invalid node name {label!r}: use 1-63 letters, digits, '_', '.', '-' "
            "(must start with a letter or digit)")
    return label


def virsh_name(name: str) -> str:
    name = (name or "").strip()
    if not _VIRSH_NAME.fullmatch(name):
        raise ValidationError(
            f"invalid VM name {name!r}: use 1-48 letters, digits, '_', '.', '-' "
            "(must start with a letter or digit)")
    return name


def lab_title(title: str) -> str:
    title = (title or "").strip()
    if not _LAB_TITLE.fullmatch(title):
        raise ValidationError(
            f"invalid lab title {title!r}: 1-64 chars, letters/digits/space/_ . - ( )")
    return title


def uuid(value: str) -> str:
    value = (value or "").strip().strip('"')
    if not _UUID.fullmatch(value):
        raise ValidationError(f"not a UUID: {value!r}")
    return value


def username(value: str) -> str:
    value = (value or "").strip()
    if not _USER.fullmatch(value):
        raise ValidationError(f"invalid user name {value!r}")
    return value


def host(value: str) -> str:
    """Management address: an IPv4/IPv6 literal or a plain DNS name."""
    value = (value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9\-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,62}))*", value) \
            and len(value) <= 253:
        return value
    raise ValidationError(f"invalid management address {value!r}")


def net_name(a: str, b: str) -> str:
    """Isolated libvirt network for a host-VM <-> host-VM link (order independent)."""
    x, y = sorted((virsh_name(a), virsh_name(b)))
    name = f"labdesk-{x}-{y}"
    if len(name) > 64:
        raise ValidationError("VM names too long for a link network name")
    return name
