"""Per-device credentials (probe.creds writer), on-device account parsing (accounts.py), the engine's handling of
plain-text device passwords (redaction) and the hover card content builder (ui/hovercard_model.py)."""
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_console import FakeConsole
from showcase_replay import SHOWCASE, fixture_text

from labdesk_app import accounts as A
from labdesk_app import config
from labdesk_app.controller import LabController
from labdesk_app.facts import CollectorState, IfFact, NodeRole
from labdesk_app.probe import creds as C
from labdesk_app.probe.redact import redact, redact_values
from labdesk_app.telemetry import IOS_FAST, IOS_SLOW, NodeTelemetry, TelemetryEngine
from labdesk_app.ui import hovercard_model as H

# ============================================================================ parsers
IOS_ALL = """\
Building configuration...

Current configuration : 2101 bytes
!
version 15.9
service timestamps log datetime msec
service password-encryption
!
hostname R9
!
enable secret 5 $1$mERr$hx5rVt7rPNoS4wqbXKX7m0
enable password 7 0822455D0A16
!
username alice privilege 15 secret 9 $9$nhEmQVczB7dqsO$X.HsgL6x1il0RxkOSSvyQYwucySCt7qFm4v7pqCxkKM
username bob secret 8 $8$dsYGNam3K1SIJO$7nv/35M/qr6t.dVc7UY9zrJDWRVqncHub1PE9UlMQFs
username carol password 7 060506324F41
username dave password 0 Cisco123
username erin privilege 1 password plainpw
username frank nopassword
username gina algorithm-type scrypt secret 9 $9$abcdefgh$ijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMN
!
interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
!
line con 0
 password 7 104D000A0618
 logging synchronous
line aux 0
line vty 0 4
 password lab123
 login
!
end
"""

IOSVL2 = """\
!
version 15.2
no service password-encryption
service compress-config
!
hostname SW1
!
enable password cisco
!
username admin privilege 15 secret 5 $1$Ux3B$xBYzm9OeJ2UjhS0BtDkNw.
!
line con 0
line vty 0 4
 login local
!
end
"""

JUNOS_SET = """\
set system root-authentication encrypted-password "$6$Y3r0zj6v$Gq1jX3cmUaR0/eOkL3T.dZpHFh2V8U6dLrxj4mVg1p.dZtDaAZ2qJxw4Zr6sXvP0V3U0YtF8bTzQx1"
set system login user admin uid 2000
set system login user admin class super-user
set system login user admin authentication encrypted-password "$1$abcdefgh$ABCDEFGHIJKLMNOPQRSTUV"
set system login user ops class read-only
set system login user ops authentication ssh-rsa "ssh-rsa AAAAB3Nza... ops@host"
"""

JUNOS_CURLY = """\
system {
    root-authentication {
        encrypted-password "$6$saltsalt$abcdefghijklmnopqrstuvwxyz0123456789"; ## SECRET-DATA
    }
    login {
        user lab {
            uid 2001;
            class super-user;
            authentication {
                encrypted-password "$5$saltsalt$abcdefghijklmnopqrstuvwxyz0123"; ## SECRET-DATA
            }
        }
    }
}
"""

FORTI = """\
config system admin
    edit "admin"
        set accprofile "super_admin"
        set vdom "root"
        set password ENC SH2nlSm9Gwqd0XyWbbGlqqT0Q9dSBYLLbtSN83IyuLRL2bVEBM6ycRZ6gWUBHo=
    next
    edit "audit"
        set accprofile "prof_admin"
        set password ENC AK1abcdefghijklmnopqrstuvwxyz0123456789
    next
    edit "lab"
        set accprofile "prof_admin"
        set password "fortilab"
    next
end
"""

HASH_BITS = ["$1$mERr", "0822455D0A16", "$9$nhEm", "$8$dsYG", "060506324F41", "104D000A0618", "$6$Y3r0", "$1$abcd",
             "SH2nlSm9", "AK1abcd", "$5$salt", "$6$salt", "$1$Ux3B"]


def _by(acc: A.DeviceAccounts):
    return {a.name: a for a in acc.accounts}


def _no_hash_kept(acc: A.DeviceAccounts):
    blob = repr(acc) + " ".join(f"{a.name} {a.scheme} {a.detail} {a.plaintext or ''} {a!r}" for a in acc.accounts)
    for h in HASH_BITS:
        assert h not in blob


def test_ios_every_storage_type():
    acc = A.parse_ios_accounts(IOS_ALL)
    by = _by(acc)
    assert acc.password_encryption is True
    assert by["enable secret"].encrypted and by["enable secret"].scheme == "type 5 MD5"
    assert by["enable secret"].detail == "encrypted (type 5 MD5)"
    assert by["enable password"].encrypted and "type 7" in by["enable password"].scheme and by["enable password"].weak
    assert by["alice"].detail == "encrypted (type 9 scrypt)" and by["alice"].privilege == 15
    assert by["bob"].scheme == "type 8 PBKDF2-SHA-256" and not by["bob"].weak
    assert by["carol"].encrypted and "reversible" in by["carol"].scheme
    assert not by["dave"].encrypted and by["dave"].plaintext == "Cisco123"
    assert by["dave"].detail == "plain text (not encrypted)"
    assert not by["erin"].encrypted and by["erin"].plaintext == "plainpw" and by["erin"].privilege == 1
    assert by["frank"].scheme == "no password"
    assert by["gina"].scheme == "type 9 scrypt"
    assert by["line con 0"].encrypted and "type 7" in by["line con 0"].scheme
    assert by["line vty 0 4"].plaintext == "lab123" and not by["line vty 0 4"].encrypted
    assert "line aux 0" not in by                         # no password on that line
    assert sorted(acc.plaintexts()) == ["Cisco123", "lab123", "plainpw"]
    _no_hash_kept(acc)
    assert "Cisco123" not in repr(by["dave"])             # repr never shows a value


def test_iosvl2_and_real_showcase_capture():
    acc = A.parse_ios_accounts(IOSVL2)
    by = _by(acc)
    assert acc.password_encryption is False
    assert by["enable password"].plaintext == "cisco" and not by["enable password"].encrypted
    assert by["admin"].detail == "encrypted (type 5 MD5)"
    _no_hash_kept(acc)
    # a real IOSv 15.9 running-config (Protocol Showcase R1)
    real = A.parse_ios_accounts(fixture_text("R1", "show running-config"))
    rb = _by(real)
    assert rb["enable secret"].scheme == "type 9 scrypt" and rb["admin"].scheme == "type 9 scrypt"
    assert rb["admin"].privilege == 15 and real.password_encryption is False and real.plaintexts() == []


def test_day0_text_secret_without_type_is_plain():
    acc = A.parse_ios_accounts("username admin privilege 15 secret cisco\nenable secret cisco\n")
    assert all(not a.encrypted and a.plaintext == "cisco" for a in acc.accounts)


def test_junos_set_and_curly():
    acc = A.parse_junos_accounts(JUNOS_SET)
    by = _by(acc)
    assert by["root"].detail == "encrypted ($6 SHA-512)"
    assert by["admin"].scheme == "$1 MD5" and by["admin"].weak
    assert by["ops"].scheme == "no password"          # ssh key only
    _no_hash_kept(acc)
    cur = _by(A.parse_junos_accounts(JUNOS_CURLY))
    assert cur["root"].scheme == "$6 SHA-512" and cur["lab"].scheme == "$5 SHA-256"
    assert A.parse_accounts(JUNOS_SET, "juniper").vendor == "juniper"


def test_fortios_enc_and_plain():
    acc = A.parse_fortios_accounts(FORTI)
    by = _by(acc)
    assert by["admin"].encrypted and by["admin"].detail.startswith("encrypted (ENC SHA-256")
    assert by["audit"].encrypted and by["audit"].weak
    assert not by["lab"].encrypted and by["lab"].plaintext == "fortilab"
    _no_hash_kept(acc)


def test_redaction_still_masks_plain_device_passwords():
    out = redact_values(redact(IOS_ALL, "cisco"), A.parse_ios_accounts(IOS_ALL).plaintexts())
    for s in ("Cisco123", "plainpw", "lab123"):
        assert s not in out
    assert "username dave password 0 <redacted>" in out


# ============================================================================ creds writer
@pytest.fixture()
def cfile(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "labdesk" / "device-creds"
    monkeypatch.setenv("LABDESK_DEVICE_CREDS", str(p))
    monkeypatch.setattr(config, "CRED_FILE", tmp_path / "no-cml-creds.env")
    assert str(Path.home() / ".config") not in str(p)
    return p


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.mark.linux_only
def test_save_round_trip_mode_and_atomic(cfile):
    C.save("R1", {"USERNAME": "admin", "PASSWORD": "pw-R1!", "ENABLE": "en-R1"})
    assert _mode(cfile) == 0o600
    c = C.load("R1")
    assert (c.username, c.password, c.enable) == ("admin", "pw-R1!", "en-R1")
    assert "pw-R1!" not in repr(c) and "pw-R1!" not in repr(C.load_detail("R1"))
    assert not [f for f in os.listdir(cfile.parent) if f.startswith(".device-creds.")]   # no temp left behind
    assert set(C.all_secret_values()) >= {"pw-R1!", "en-R1"}
    d = C.load_detail("R1")
    assert not d.uses_defaults and d.source("PASSWORD") == "device"


def test_save_preserves_other_keys_and_comments(cfile):
    cfile.parent.mkdir(parents=True)
    cfile.write_text("# my notes\nDEFAULT_USERNAME=admin\nDEFAULT_ENABLE=cisco\n\nSW1_ENABLE=sw1secret\n"
                     "R1_ENABLE=old\n# trailing comment\n")
    os.chmod(cfile, 0o600)
    C.save("R1", {"USERNAME": "", "PASSWORD": "new pass", "ENABLE": "newen"})
    text = cfile.read_text()
    assert "# my notes" in text and "# trailing comment" in text and "SW1_ENABLE=sw1secret" in text
    assert "R1_ENABLE=newen" in text and "R1_ENABLE=old" not in text and "R1_PASSWORD=new pass" in text
    assert "R1_USERNAME" not in text                       # empty = not stored
    assert text.index("R1_ENABLE=newen") < text.index("# trailing comment")   # rewritten in place
    c = C.load("R1")
    assert (c.username, c.password, c.enable) == ("admin", "new pass", "newen")   # user falls back to default
    assert C.load("SW1").enable == "sw1secret"


def test_use_defaults_drops_node_keys_and_writes_defaults(cfile):
    C.save("R1", {"USERNAME": "u1", "PASSWORD": "p1", "ENABLE": "e1"})
    C.save("SW2", {"ENABLE": "e2"})
    C.save("R1", {"USERNAME": "admin", "PASSWORD": "dpw", "ENABLE": "cisco"}, use_defaults=True)
    text = cfile.read_text()
    assert "R1_" not in text and "SW2_ENABLE=e2" in text and "DEFAULT_PASSWORD=dpw" in text
    d = C.load_detail("R1")
    assert d.uses_defaults and d.creds.enable == "cisco" and d.source("ENABLE") == "default"
    assert C.load("SW2").enable == "e2" and C.load("SW2").username == "admin"
    assert _mode(cfile) == 0o600


@pytest.mark.linux_only
def test_save_fixes_a_loose_mode_and_rejects_bad_values(cfile):
    cfile.parent.mkdir(parents=True)
    cfile.write_text("DEFAULT_ENABLE=x\n")
    os.chmod(cfile, 0o644)
    assert C.load_detail("R1").error                       # refused to read a group/world readable file
    C.save("R1", {"ENABLE": "abc"})
    assert _mode(cfile) == 0o600 and C.load("R1").enable == "abc"
    for bad in ("two\nlines", " lead", "trail ", '"quoted"'):
        with pytest.raises(ValueError):
            C.save("R1", {"PASSWORD": bad})
    with pytest.raises(ValueError):
        C.save("R1", {"USERNAME": "bad user"})
    assert C.load("R1").enable == "abc"                    # a refused save changes nothing


# ============================================================================ engine: accounts + redaction
LAB = "fb0393aa-b6de-45fa-b056-f8607420f84f"
UUID_TO_LABEL = {"ad62403d-e0e6-4c80-bf90-5dd999ac9a9f": "R1", "225663e3-de05-4d57-9f80-b6c5228c9b68": "R2",
                 "539f48a0-8da7-4be1-bc02-6883ed1d22d6": "R3", "d5711300-1654-4b4c-baeb-e0f7ac73d6c9": "R4"}


def test_engine_reads_accounts_and_never_logs_plain_device_passwords(tmp_path, monkeypatch):
    from test_telemetry_engine import FakeClient
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setenv("LABDESK_DEVICE_CREDS", str(tmp_path / "device-creds"))
    rc = fixture_text("R1", "show running-config").replace(
        "no aaa new-model", "username guest privilege 1 password 0 Gu3stPlain\nno aaa new-model")

    def factory(uuid, user):
        r = UUID_TO_LABEL[uuid]
        outs = {c: fixture_text(r, c) or "" for c in IOS_FAST + IOS_SLOW + ["show running-config"]}
        if r == "R1":
            outs["show running-config"] = rc
        return FakeConsole(r, "priv", outs)

    ctl = LabController(FakeClient(), config.State(tmp_path / "state.json"), telemetry=False)
    ctl.telemetry = TelemetryEngine(ctl.client, transport_factory=factory, probe_log_dir=tmp_path / "probe",
                                    console_scan=lambda ex: set(), creds_loader=lambda label: C.DeviceCreds())
    ctl.lab_id = LAB
    try:
        end = time.time() + 40
        r1 = None
        while time.time() < end:
            v = ctl.poll()
            r1 = next(n for n in v.nodes if n.label == "R1")
            if r1.tele is not None and r1.tele.accounts is not None:
                break
            time.sleep(0.3)
        assert r1.tele.accounts is not None, "config tier never ran"
        by = _by(r1.tele.accounts)
        assert by["admin"].detail == "encrypted (type 9 scrypt)" and by["guest"].plaintext == "Gu3stPlain"
        assert r1.tele.accounts_age_s is not None
        log = (tmp_path / "probe" / "R1.log").read_text()
        assert "Gu3stPlain" not in log and "$9$" not in log
        assert "Gu3stPlain" not in (r1.tele.config_text or "")
        assert "Gu3stPlain" in ctl.telemetry._secret_values()
    finally:
        ctl.shutdown()


def test_secret_cache_follows_creds_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LABDESK_DEVICE_CREDS", str(tmp_path / "device-creds"))
    monkeypatch.setattr(config, "CRED_FILE", tmp_path / "none.env")
    eng = TelemetryEngine(None, transport_factory=lambda u, us: None, probe_log_dir=tmp_path / "p",
                          console_scan=lambda ex: set(), autostart=False)
    assert "brand-new-pw" not in eng._secret_values()
    C.save("R1", {"PASSWORD": "brand-new-pw"})
    eng.credentials_changed("R1")
    assert "brand-new-pw" in eng._secret_values()


# ============================================================================ hover card content
def _nv(tele=None, state="BOOTED", key="cml:r1", label="R1"):
    dtype = SimpleNamespace(title="Cisco IOSv (router)", backend="cml", vendor="cisco", key="iosv",
                            node_definition="iosv")
    return SimpleNamespace(key=key, label=label, dtype=dtype, state=state, tele=tele, chips=[])


def _tele(n_ifaces=3, accounts=None, note=""):
    ifs = [IfFact(f"GigabitEthernet0/{i}", f"Gi0/{i}", f"10.0.{i}.1" if i < 2 else None, 30 if i < 2 else None,
                  "up", "up" if i != 1 else "down", "up") for i in range(n_ifaces)]
    nt = NodeTelemetry("cml:r1", "R1", NodeRole.ROUTER, "cisco", CollectorState.OK, last_probe_age_s=7.0)
    nt.ifaces = ifs
    nt.ospf_nbrs = [{"neighbor_id": "2.2.2.2", "name": "R2", "state": "FULL/DR", "adj_state": "FULL"},
                    {"neighbor_id": "3.3.3.3", "name": "R3", "state": "INIT/-", "adj_state": "INIT"}]
    nt.accounts, nt.accounts_age_s, nt.accounts_note = accounts, (12.0 if accounts else None), note
    return nt


def _detail(**own):
    return C.CredsDetail(C.DeviceCreds(own.get("USERNAME"), own.get("PASSWORD"), own.get("ENABLE")), own, {})


def test_card_shows_live_facts_and_logins():
    acc = A.parse_ios_accounts(IOS_ALL)
    m = H.build_card(_nv(_tele(accounts=acc)), creds=_detail(USERNAME="admin", PASSWORD="pw1", ENABLE="cisco"),
                     show_passwords=True, mgmt_ip="192.168.122.50")
    t = m.text()
    assert m.title == "R1" and m.subtitle == "Cisco IOSv · CML" and m.state_word == "CML: Booted"
    assert "Mgmt IP: 192.168.122.50 (saved in LabDesk)" in t and "Live data: read 7 s ago" in t
    ifs = m.section("Interfaces")
    assert ifs.aside == "2 of 3 up" and [r.text for r in ifs.rows][:2] == ["Gi0/0", "Gi0/1"]
    assert ifs.rows[0].value == "10.0.0.1/30" and ifs.rows[1].glyph == "proto_down"
    proto = m.section("Protocols").rows[0]
    assert proto.tag == "OSPF" and proto.value == "1/2 neighbours FULL" and proto.tone == "warn"
    login = m.section("Login").rows
    assert login[0].value == "admin / pw1" and login[1].value == "cisco"
    dev = {r.text: r for r in m.section("On the device").rows}
    assert dev["alice"].value == "encrypted" and dev["alice"].note == "type 9 scrypt"
    assert dev["enable password"].value == "encrypted" and "reversible" in dev["enable password"].note
    assert dev["dave"].value == "Cisco123" and dev["dave"].note == "plain text, not encrypted"
    assert m.section("On the device").note == "service password-encryption: on"
    assert m.section("On the device").aside == "read 12 s ago"
    for h in HASH_BITS:
        assert h not in t                                   # never a hash on the card


def test_card_masks_passwords_when_toggle_off():
    acc = A.parse_ios_accounts(IOS_ALL)
    m = H.build_card(_nv(_tele(accounts=acc)), creds=_detail(USERNAME="admin", PASSWORD="pw1", ENABLE="cisco"),
                     show_passwords=False)
    t = m.text()
    for s in ("pw1", "Cisco123", "plainpw", "lab123"):
        assert s not in t
    assert "admin / " + H.MASK in t and m.section("Login").aside == "hidden"
    assert {r.text: r for r in m.section("On the device").rows}["dave"].value == H.MASK


def test_card_hints_when_nothing_is_known():
    m = H.build_card(_nv(_tele(note="needs enable secret")), creds=_detail(), show_passwords=True)
    assert "no credentials saved — right-click ▸ Credentials…" in m.section("Login").note
    dev = m.section("On the device")
    assert "needs enable secret" in dev.note and dev.note_tone == "warn" and not dev.rows
    off = H.build_card(_nv(None, state="STOPPED"), creds=None)
    assert off.state_word == "CML: Stopped" and off.section("Interfaces") is None and off.section("Protocols") is None
    kvm = H.build_card(_nv(NodeTelemetry("kvm:JR1", "JR1", NodeRole.ROUTER, "juniper", CollectorState.UNVERIFIED),
                           key="kvm:JR1", label="JR1"), creds=None)
    assert kvm.section("On the device").note.startswith("not read")
    assert "Live data: not read" in kvm.text()


def test_card_caps_interfaces():
    m = H.build_card(_nv(_tele(n_ifaces=13)), creds=None)
    ifs = m.section("Interfaces")
    assert len(ifs.rows) == H.MAX_IFACES and ifs.more == 5
    assert [r.value for r in ifs.rows[:2]] == ["10.0.0.1/30", "10.0.1.1/30"]     # addressed first


def test_hover_card_widget_paints():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtCore import QPoint
    app = QApplication.instance() or QApplication([])
    from labdesk_app.ui.hovercard import HoverCard
    host = QWidget()
    host.resize(900, 700)
    host.show()
    card = HoverCard(host)
    m = H.build_card(_nv(_tele(accounts=A.parse_ios_accounts(IOS_ALL))), creds=_detail(USERNAME="a", PASSWORD="b"))
    card.show_card(m, None, QPoint(100, 100))
    assert card.isVisible() and card.height() > 200 and card.width() == HoverCard.W + 2 * HoverCard.SH
    assert card.geometry().right() <= host.width() and card.geometry().bottom() <= host.height() + HoverCard.SH
    img = card.grab()
    assert not img.isNull()
    card.reposition()
    assert not card.isVisible()
    app.processEvents()


def test_running_config_gets_a_serial_sized_timeout():
    """Live IOSv R1: 'show running-config' took 16.8 s, IOSvL2 SW1 20.1 s - longer than the 15 s per-command default."""
    from labdesk_app.probe.session import IosDialect
    from labdesk_app.telemetry import CONFIG_TIMEOUT_S
    d = IosDialect()
    assert "show running-config".startswith(d.slow_prefixes) and d.slow_cmd_timeout >= 30
    assert CONFIG_TIMEOUT_S >= 40
