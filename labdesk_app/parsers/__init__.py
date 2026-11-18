"""Vendor CLI parsers: ios, junos, fortios.  See common.ParseResult."""
from . import common, fortios, ios, junos  # noqa: F401
from .common import ParseResult, expand_iface, iface_key, short_iface, split_syslog  # noqa: F401
