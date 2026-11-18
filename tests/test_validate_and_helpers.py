import os
import subprocess
import sys

import pexpect
import pytest

from labdesk_app import catalog, config, validate
from labdesk_app.launchers import ASKPASS, TABEXEC


@pytest.mark.parametrize("ok", ["R1", "core-sw.1", "BR-JR1", "a" * 63])
def test_cml_label_ok(ok):
    assert validate.cml_label(ok) == ok


@pytest.mark.parametrize("bad", ["", " ", "-R1", "R 1", "R1;x", "R/1", "a" * 64, "R1\nshow"])
def test_cml_label_bad(bad):
    with pytest.raises(validate.ValidationError):
        validate.cml_label(bad)


def test_net_name_order_independent():
    assert validate.net_name("JR1", "FGT1") == validate.net_name("FGT1", "JR1") == "labdesk-FGT1-JR1"


def test_host_validation():
    assert validate.host(" 10.0.0.1 ") == "10.0.0.1"
    assert validate.host("r1.lab") == "r1.lab"
    for bad in ("-oX", "a b", "1.2.3.4;id", ""):
        with pytest.raises(validate.ValidationError):
            validate.host(bad)


def test_next_label_and_default_config():
    assert catalog.next_label("R", {"R1", "R2", "R4"}) == "R3"
    cfg = catalog.default_config(catalog.BY_KEY["iosv"], "R7")
    assert cfg.startswith("hostname R7\n")
    assert catalog.default_config(catalog.BY_KEY["ext_bridge"], "BR1") == "System Bridge"


def test_type_for_cml_node():
    assert catalog.type_for_cml_node({"node_definition": "external_connector",
                                      "configuration": [{"name": "default", "content": "NAT"}]}).key == "ext_nat"
    assert catalog.type_for_cml_node({"node_definition": "external_connector",
                                      "configuration": "System Bridge"}).key == "ext_bridge"
    assert catalog.type_for_cml_node({"node_definition": "iosv"}).key == "iosv"


def test_load_env_file(tmp_path):
    p = tmp_path / "e.env"
    p.write_text("# c\n\nA=1\nB = 'two'\nC=\"x=y\"\n")
    assert config.load_env_file(p) == {"A": "1", "B": "two", "C": "x=y"}


def test_state_roundtrip(tmp_path):
    s = config.State(tmp_path / "s.json")
    s.lab("L1")["mgmt"]["cml:n1"] = {"ip": "1.2.3.4", "user": "admin"}
    s.save()
    s2 = config.State(tmp_path / "s.json")
    assert s2.lab("L1")["mgmt"]["cml:n1"]["ip"] == "1.2.3.4"


def test_askpass_reads_named_key_only(tmp_path):
    env_file = tmp_path / "c.env"
    env_file.write_text("CML_ADMIN_PASS=topsecret\nOTHER=nope\n")
    env = {"CML_CRED_FILE": str(env_file), "LABDESK_ASKPASS_KEY": "CML_ADMIN_PASS", "PATH": os.environ["PATH"]}
    out = subprocess.run([sys.executable, str(ASKPASS), "admin@cml's password:"], env=env,
                         capture_output=True, text=True)
    assert out.stdout == "topsecret\n"
    out = subprocess.run([sys.executable, str(ASKPASS), "Are you sure (yes/no/[fingerprint])?"], env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "no"
    env["LABDESK_ASKPASS_KEY"] = "OTHER"
    out = subprocess.run([sys.executable, str(ASKPASS), "password:"], env=env, capture_output=True, text=True)
    assert out.returncode == 1 and out.stdout == ""


def test_tabexec_runs_whitelisted_argv_on_pty():
    child = pexpect.spawn("/usr/bin/python3", ["-I", "-S", str(TABEXEC)], encoding="utf-8", timeout=10)
    child.sendline('{"title":"T","argv":["ssh","-V"],"env":{}}')
    child.expect("OpenSSH")
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0
    assert '"argv"' not in child.before          # request line not echoed


def test_tabexec_refuses_other_programs():
    child = pexpect.spawn("/usr/bin/python3", ["-I", "-S", str(TABEXEC)], encoding="utf-8", timeout=10)
    child.sendline('{"title":"T","argv":["bash","-c","id"],"env":{}}')
    child.expect("refusing to run")
    child.sendline("")
    child.expect(pexpect.EOF)
