"""D1/D10: burst etiquette against a scripted console (no network)."""
import pytest
from fake_console import FakeConsole
from showcase_replay import fixture_text

from labdesk_app.probe.creds import DeviceCreds
from labdesk_app.probe.session import BurstStatus, IosDialect, run_burst
from labdesk_app.probe.transport import termserver_argv

CMDS = ["show ip interface brief", "show ip ospf neighbor", "show standby brief"]


def outputs(r="R1"):
    return {c: fixture_text(r, c) for c in CMDS}


@pytest.mark.linux_only
def test_argv_is_batchmode_tt_and_key_based():
    argv = termserver_argv("ad62403d-e0e6-4c80-bf90-5dd999ac9a9f", "admin")
    assert argv[0] == "ssh" and argv.count("-t") == 2
    assert argv[1:3] == ["-t", "-o"] and "BatchMode=yes" in argv
    assert argv.index("ServerAliveInterval=5") < argv.index("ServerAliveInterval=30")   # first -o wins
    assert argv[-2:] == ["connect", "ad62403d-e0e6-4c80-bf90-5dd999ac9a9f"]


def test_clean_burst_at_privileged_prompt():
    con = FakeConsole("R1", "priv", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert res.status == BurstStatus.OK and res.hostname == "R1" and res.mode_found == "priv"
    assert res.outputs["show ip ospf neighbor"].text.startswith("Neighbor ID")
    assert res.outputs["show standby brief"].text.strip().endswith("192.168.10.1")
    sent = con.sent_text
    assert "terminal length 0" in sent and "terminal length 24" in sent and "terminal width 80" in sent
    assert "enable" not in sent and "exit" not in sent        # found logged in: never exit
    assert con.closed


def test_empty_output_is_empty_text_not_none():
    con = FakeConsole("R3", "user", {"show standby brief": ""})
    res = run_burst(con, IosDialect(), ["show standby brief"], timeout=5)
    assert res.status == BurstStatus.OK and res.outputs["show standby brief"].text == ""


def test_config_mode_aborts_without_sending_anything_more():
    con = FakeConsole("R1", "config", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert res.status == BurstStatus.BUSY and "configuration mode" in res.reason
    assert con.sent_text == "\r"                               # only the probe CR - never 'end'


def test_pager_remnant_aborts():
    con = FakeConsole("R1", "more", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert res.status == BurstStatus.BUSY and con.sent_text == "\r"


def test_foreign_password_prompt_aborts():
    con = FakeConsole("R1", "password", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert res.status == BurstStatus.BUSY and con.sent_text == "\r"


def test_logged_out_line_is_logged_out_again():
    con = FakeConsole("R1", "logged_out", outputs())
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert res.status == BurstStatus.OK and res.mode_found == "logged_out"
    assert con.sent_text.rstrip("\r").endswith("exit")


def test_interleaved_syslog_is_stripped_and_returned():
    line = "*Nov 17 04:12:33.123: %OSPF-5-ADJCHG: Process 1, Nbr 3.3.3.3 on GigabitEthernet0/2 from FULL to DOWN"
    con = FakeConsole("R1", "priv", outputs(), syslog_in={"show ip interface brief": line})
    res = run_burst(con, IosDialect(), CMDS, timeout=5)
    assert "%OSPF" not in res.outputs["show ip interface brief"].text
    assert res.syslog and res.syslog[0].mnemonic == "ADJCHG" and res.syslog[0].ts == "Nov 17 04:12:33.123"


def test_timeout_marks_needs_recovery():
    con = FakeConsole("R1", "priv", outputs(), hang={"show ip ospf neighbor"})
    res = run_burst(con, IosDialect(), CMDS, timeout=2.0, cmd_timeout=5.0)
    assert res.status == BurstStatus.TIMEOUT and res.needs_recovery


def test_command_without_sentinel_is_partial():
    con = FakeConsole("R1", "priv", outputs(), hang={"show ip ospf neighbor"})
    res = run_burst(con, IosDialect(), CMDS, timeout=10.0, cmd_timeout=0.5)
    assert res.status in (BurstStatus.PARTIAL, BurstStatus.TIMEOUT)
    assert res.outputs["show ip ospf neighbor"].text is None


def test_recovery_sends_single_ctrl_c_first():
    con = FakeConsole("R1", "priv", outputs())
    run_burst(con, IosDialect(), CMDS[:1], timeout=5, needs_recovery=True)
    assert con.written[0] == b"\x03"


def test_enable_only_when_needed_and_disabled_after():
    con = FakeConsole("R1", "user", {"show running-config": "hostname R1\n"}, enable_secret="s3cret")
    res = run_burst(con, IosDialect(), ["show running-config"], DeviceCreds(enable="s3cret"), timeout=5)
    assert res.status == BurstStatus.OK and res.enabled
    assert "disable" in con.sent_text and con.mode == "user"
    assert "s3cret" not in res.outputs["show running-config"].text


def test_privileged_command_without_secret_reports_need():
    con = FakeConsole("R1", "user", {"show ip interface brief": fixture_text("R1", "show ip interface brief")})
    res = run_burst(con, IosDialect(), ["show ip interface brief", "show running-config"], timeout=5)
    assert res.outputs["show running-config"].error == "needs enable secret"
    assert res.outputs["show ip interface brief"].text
    assert "enable" not in con.sent_text


def test_invalid_input_output_kept_for_parser():
    from labdesk_app.parsers import ios
    con = FakeConsole("R1", "priv", {})
    res = run_burst(con, IosDialect(), ["show vrrp brief"], timeout=5)
    out = res.outputs["show vrrp brief"]
    assert out.text is not None and not ios.parse_vrrp_brief(out.text).ok


def test_ping_flushes_typeahead_so_sentinel_waits_for_prompt():
    con = FakeConsole("R1", "priv", {"ping 8.8.8.8": fixture_text("R1", "ping 8.8.8.8")})
    res = run_burst(con, IosDialect(), ["ping 8.8.8.8"], timeout=8)
    assert res.status == BurstStatus.OK and "Success rate is 100 percent" in res.outputs["ping 8.8.8.8"].text


def test_user_mode_ping_and_trace_drop_options_iosv_rejects():
    """IOSv '>' answers '% Invalid input' to 'ping X repeat N timeout 1' and to any traceroute option
    (seen live on the showcase with no enable secret stored): send the bare form, key by the original."""
    ping = "ping 8.8.8.8 repeat 3 timeout 1"
    trace = "traceroute 8.8.8.8 numeric timeout 1 probe 1 ttl 1 8"
    con = FakeConsole("R1", "user", {"ping 8.8.8.8": fixture_text("R1", "ping 8.8.8.8"),
                                     "traceroute 8.8.8.8": "Type escape sequence to abort.\n  1 10.0.13.2 4 msec"})
    res = run_burst(con, IosDialect(), [ping, trace], timeout=8)
    assert res.status == BurstStatus.OK and not res.enabled
    assert "Success rate is 100 percent" in res.outputs[ping].text
    assert "10.0.13.2" in res.outputs[trace].text
    assert "repeat" not in con.sent_text and "numeric" not in con.sent_text


def test_priv_mode_keeps_ping_options():
    cmd = "ping 8.8.8.8 repeat 3 timeout 1"
    con = FakeConsole("R1", "priv", {cmd: fixture_text("R1", "ping 8.8.8.8")})
    res = run_burst(con, IosDialect(), [cmd], timeout=8)
    assert res.status == BurstStatus.OK and cmd in con.sent_text
