"""D10 redaction (showcase configs) + D8 link-condition validation."""
import os

import pytest
from showcase_replay import fixture_text

from labdesk_app.cml_api import CMLError, link_condition_body
from labdesk_app.probe import creds
from labdesk_app.probe.redact import MASK, redact, redact_values


@pytest.mark.parametrize("r", ["R1", "R2", "R3", "R4"])
def test_showcase_configs_redacted(r):
    text = redact(fixture_text(r, "show running-config"))
    assert "$9$" not in text
    assert f"enable secret 9 {MASK}" in text and f"username admin privilege 15 secret 9 {MASK}" in text
    assert "no service password-encryption" in text            # not a secret


def test_vendor_secrets():
    s = redact('set password ENC SGVsbG8gV29ybGQgSGVsbG8=\nencrypted-password "$6$abc$def";\n'
               "snmp-server community public RO\n key-string 7 0011\n")
    assert "SGVsbG8" not in s and "$6$" not in s and "public" not in s and "0011" not in s
    assert redact_values("pw is hunter22", ["hunter22"]) == f"pw is {MASK}"


@pytest.mark.linux_only
def test_device_creds_mode_and_repr(tmp_path):
    p = tmp_path / "device-creds"
    p.write_text("DEFAULT_ENABLE=topsecret\nR1_ENABLE=other\n")
    os.chmod(p, 0o644)
    with pytest.raises(creds.CredsError):
        creds.load("R1", p)
    os.chmod(p, 0o600)
    c = creds.load("R1", p)
    assert c.enable == "other" and creds.load("R2", p).enable == "topsecret"
    assert "other" not in repr(c) and "topsecret" not in repr(c)
    assert creds.load("R1", tmp_path / "missing") == creds.DeviceCreds()


def test_link_condition_body():
    assert link_condition_body({"latency": 50, "loss": 2.5}) == {"latency": 50, "loss": 2.5, "enabled": True}
    with pytest.raises(CMLError):
        link_condition_body({"latency": 20000})
    with pytest.raises(CMLError):
        link_condition_body({"bogus": 1})
