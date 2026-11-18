"""First-login defaults (default_logins.py), day-0 parsing, the device-creds backfill (never overwrites, mode
0600) and what the hover card / console hint say.  Temp dirs only - never the real device-creds."""
import os
import stat
from types import SimpleNamespace

import pytest

from labdesk_app import default_logins as DL
from labdesk_app.catalog import BY_KEY, IOS_BASE_CONFIG, default_config
from labdesk_app.controller import LabController
from labdesk_app.probe import creds as C
from labdesk_app.ui import hovercard_model as H


@pytest.fixture
def cfile(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "device-creds"
    monkeypatch.setenv("LABDESK_DEVICE_CREDS", str(p))
    return p


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _nv(label, dkey, cfg=None, key=None, nd=None):
    dtype = BY_KEY[dkey]
    raw = {"node_definition": nd or dtype.node_definition}
    if cfg is not None:
        raw["configuration"] = [{"name": "ios_config.txt", "content": cfg}]
    if dtype.backend == "kvm":
        raw = {"kind": dkey}
    return SimpleNamespace(key=key or f"cml:{label}", label=label, dtype=dtype, state="BOOTED", tele=None,
                           chips=[], raw=raw)


# ------------------------------------------------------------------ table
def test_table_matches_the_sources():
    ios = DL.PLATFORM_DEFAULTS["iosv"]
    assert (ios.username, ios.password, ios.enable) == ("admin", "cisco", "cisco")
    # the LabDesk day-0 template really sets these
    assert "username admin privilege 15 secret cisco" in IOS_BASE_CONFIG and "enable secret cisco" in IOS_BASE_CONFIG
    for nd in ("iosvl2", "iol-xe", "ioll2-xe"):
        assert DL.PLATFORM_DEFAULTS[nd] == ios
    lin = DL.PLATFORM_DEFAULTS["alpine"]
    assert (lin.username, lin.password, lin.enable) == ("cisco", "cisco", None)
    jr = DL.PLATFORM_DEFAULTS["vjunos"]
    assert jr.username == "root" and jr.password is None and "plain-text-password" in jr.note
    fg = DL.PLATFORM_DEFAULTS["fortios"]
    assert fg.username == "admin" and fg.password is None and "new one" in fg.note
    for nd in ("unmanaged_switch", "external_connector"):
        assert DL.first_login(nd, "x") is None and not DL.has_login(nd)
    assert len(DL.DOC_ROWS) >= 6


def test_labdesk_day0_template_parses_to_the_table_value():
    for k in ("iosv", "iosvl2", "iol-xe"):
        fl = DL.first_login(BY_KEY[k].node_definition, default_config(BY_KEY[k], "R1"))
        assert (fl.username, fl.password, fl.enable) == ("admin", "cisco", "cisco") and fl.source == "day-0 config"
        assert fl.console_note and "SSH / telnet" in fl.note        # line con 0 has no 'login' in the template
    fl = DL.first_login("alpine", default_config(BY_KEY["alpine"], "H1"))   # LabDesk alpine: hostname only
    assert (fl.username, fl.password) == ("cisco", "cisco")


# ------------------------------------------------------------------ day-0 parsing
def test_day0_ios_plain_hashed_and_blank():
    cfg = ("hostname X\nusername bob secret 9 $9$abc$def\nusername ops privilege 15 password 0 Ops1\n"
           "enable password en1\n")
    fl = DL.parse_day0("iosv", cfg)
    assert (fl.username, fl.password, fl.enable) == ("ops", "Ops1", "en1") and not fl.password_encrypted
    fl = DL.parse_day0("iosv", "username admin privilege 15 secret 5 $1$xx$yy\nenable secret 9 $9$a$b\n")
    assert fl.username == "admin" and fl.password is None and fl.password_encrypted and fl.enable_encrypted
    assert DL.ENCRYPTED in fl.note
    assert "$1$" not in repr(fl) and "$1$" not in fl.summary()
    blank = DL.first_login("iosv", [{"name": "ios_config.txt", "content": "hostname R9\n"}])
    assert blank.empty and blank.has_enable and "press Enter" in blank.note
    assert blank.summary() == "no login (press Enter) · enable: no password"
    assert DL.first_login("iosv", None).username == "admin"          # no config known: LabDesk's template
    # enable secret wins over enable password
    fl = DL.parse_day0("iosv", "enable password weak\nenable secret strong\n")
    assert fl.enable == "strong" and fl.username is None


def test_day0_linux_and_cloud_init():
    fl = DL.parse_day0("alpine", "hostname PC-DATA\nUSERNAME=lab\nPASSWORD=Lab-pw1\nip link set eth0 up\n")
    assert (fl.username, fl.password) == ("lab", "Lab-pw1")
    assert DL.parse_day0("alpine", "hostname PC\n") is None
    assert DL.first_login("alpine", "hostname PC\n").username == "cisco"          # table fallback
    ub = DL.parse_day0("ubuntu", "#cloud-config\nsystem_info:\n  default_user:\n    name: cisco\npassword: cisco\n")
    assert (ub.username, ub.password) == ("cisco", "cisco")


def test_kvm_env_overrides():
    env = {"JUNOS_ADMIN_USER": "admin", "JUNOS_ADMIN_PASS": "Jx-secret-1"}
    fl = DL.kvm_default("vjunos", env)
    assert fl.username == "admin" and fl.password == "Jx-secret-1" and fl.source == "cml-credentials.env"
    assert "Jx-secret-1" not in repr(fl)
    assert DL.kvm_default("vjunos", {}).username == "root"
    assert DL.kvm_default("fortios", {"FGT_ADMIN_USER": "admin", "FGT_ADMIN_PASS": "F1"}).password == "F1"
    assert DL.for_node(_nv("PE", "vjunos", key="kvm:PE"), env).password == "Jx-secret-1"


# ------------------------------------------------------------------ backfill
@pytest.mark.linux_only
def test_seed_creates_0600_and_never_overwrites(cfile):
    assert C.seed("R1", {"USERNAME": "admin", "PASSWORD": "cisco", "ENABLE": "cisco"})
    assert _mode(cfile) == 0o600
    d = C.load_detail("R1")
    assert d.seeded and (d.creds.username, d.creds.password, d.creds.enable) == ("admin", "cisco", "cisco")
    C.save("R2", {"USERNAME": "me", "PASSWORD": "mine", "ENABLE": "mine-en"})        # the user's own
    assert not C.seed("R2", {"USERNAME": "admin", "PASSWORD": "cisco", "ENABLE": "cisco"})
    assert C.load("R2").password == "mine" and not C.load_detail("R2").seeded
    assert not C.seed("R1", {"USERNAME": "other", "PASSWORD": "x"})                   # seeded once only
    assert C.load("R1").username == "admin"
    # the user edits a seeded node: it becomes 'saved'
    C.save("R1", {"USERNAME": "admin", "PASSWORD": "new", "ENABLE": "cisco"})
    assert not C.load_detail("R1").seeded and C.load("R1").password == "new"
    # cleared by the user: remembered, not filled in again
    C.save("R1", {"USERNAME": "", "PASSWORD": "", "ENABLE": ""})
    assert not C.seed("R1", {"USERNAME": "admin"})
    # reset to default
    C.reset_to_default("R1", {"USERNAME": "admin", "PASSWORD": "cisco", "ENABLE": "cisco"})
    d = C.load_detail("R1")
    assert d.seeded and d.creds.password == "cisco" and _mode(cfile) == 0o600
    assert not [f for f in os.listdir(cfile.parent) if f.startswith(".device-creds.")]


def test_seed_leaves_shared_defaults_alone(cfile):
    C.save(None, {"USERNAME": "admin", "PASSWORD": "shared", "ENABLE": "shared-en"})
    assert C.seed("SW1", {"USERNAME": "admin", "PASSWORD": "cisco", "ENABLE": "cisco"})
    assert C.load("SW1").password == "shared"                      # DEFAULT_* (the user's) still wins
    assert "SW1_PASSWORD" not in cfile.read_text()


@pytest.mark.linux_only
def test_seed_skips_a_file_with_a_loose_mode(cfile):
    cfile.parent.mkdir(parents=True)
    cfile.write_text("R9_ENABLE=x\n")
    os.chmod(cfile, 0o644)
    assert not C.seed("R1", {"USERNAME": "admin"})
    assert "R1_" not in cfile.read_text()


def test_controller_backfill(cfile, monkeypatch):
    ctl = LabController.__new__(LabController)
    ctl.lab_id = "lab1"
    ctl._first_login_seen, ctl.first_login_seeded = set(), []
    monkeypatch.setattr(DL, "load_env", lambda: {"JUNOS_ADMIN_USER": "admin", "JUNOS_ADMIN_PASS": "Jpw-9"})
    C.save("R2", {"USERNAME": "keep", "PASSWORD": "keep-pw", "ENABLE": "keep-en"})
    views = [_nv("R1", "iosv", IOS_BASE_CONFIG.format(label="R1")), _nv("R2", "iosv", IOS_BASE_CONFIG),
             _nv("PC-DATA", "alpine", "hostname PC-DATA\nUSERNAME=cisco\nPASSWORD=cisco\n", nd="alpine"),
             _nv("SW-DATA", "unmanaged_switch", ""), _nv("PE", "vjunos", key="kvm:PE"),
             _nv("R9", "iosv", "hostname R9\n")]
    done = ctl.backfill_first_logins(views)
    assert done == ["R1", "PC-DATA", "PE", "R9"]
    assert C.load("R1").enable == "cisco" and C.load("PC-DATA").password == "cisco"
    assert C.load("R2").password == "keep-pw"                       # never overwritten
    assert C.load("PE").password == "Jpw-9" and C.load_detail("PE").seeded
    assert C.load_detail("R9").seeded and not C.load_detail("R9").own   # blank IOSv: marker only
    assert "SW_DATA" not in cfile.read_text()
    assert ctl.pop_first_login_seeded() == done and ctl.pop_first_login_seeded() == []
    assert ctl.backfill_first_logins(views) == []                  # once per node and session


# ------------------------------------------------------------------ hover card + console hint
def test_hover_card_login_rows(cfile):
    nv = _nv("R1", "iosv", IOS_BASE_CONFIG.format(label="R1"))
    first = DL.for_node(nv)
    # nothing saved yet: the table / day-0 value, tagged default
    m = H.build_card(nv, creds=C.load_detail("R1"), first_login=first)
    rows = m.section("Login").rows
    assert rows[0].text == "LabDesk will try" and rows[0].value == "admin / cisco" and rows[0].badge == "default"
    assert rows[1].text == "enable" and rows[1].value == "cisco"
    assert "LabDesk will try: admin / cisco [default]" in m.text()
    assert "default, not read from device" in m.section("Login").note
    # seeded -> still default; edited -> saved
    C.seed("R1", first.creds_values())
    assert H.build_card(nv, creds=C.load_detail("R1"), first_login=first).section("Login").rows[0].badge == "default"
    C.save("R1", {"USERNAME": "admin", "PASSWORD": "mine", "ENABLE": "cisco"})
    r = H.build_card(nv, creds=C.load_detail("R1"), first_login=first).section("Login").rows[0]
    assert r.value == "admin / mine" and r.badge == "saved"
    # show passwords off
    m = H.build_card(nv, creds=C.load_detail("R1"), first_login=first, show_passwords=False)
    assert "mine" not in m.text() and m.section("Login").aside == "hidden"


def test_hover_card_blank_ios_vjunos_and_hashed(cfile):
    blank = _nv("R9", "iosv", "hostname R9\n")
    s = H.build_card(blank, creds=C.load_detail("R9"), first_login=DL.for_node(blank)).section("Login")
    assert s.rows[0].value == "no login (press Enter)" and s.rows[1].value == "no password"
    assert "no password until you configure one" in s.note
    jr = _nv("JR1", "vjunos", key="kvm:JR1")
    s = H.build_card(jr, creds=C.load_detail("JR1"), first_login=DL.for_node(jr, {})).section("Login")
    assert s.rows[0].value == "root / (no password)" and len(s.rows) == 1 and "amnesiac" in s.note
    hashed = _nv("R8", "iosv", "username admin privilege 15 secret 9 $9$aa$bb\nenable secret 5 $1$cc$dd\n")
    t = H.build_card(hashed, creds=C.load_detail("R8"), first_login=DL.for_node(hashed)).text()
    assert "admin / set in config (encrypted)" in t and "enable: set in config (encrypted)" in t
    assert "$9$" not in t and "$1$" not in t


def test_console_hint_text():
    fl = DL.PLATFORM_DEFAULTS["iosv"]
    con = " · console: no username, press Enter, then 'enable'"
    assert DL.console_hint("R1", fl) == "R1 first login: admin / cisco (enable: cisco)" + con
    assert DL.console_hint("PC-DATA", DL.PLATFORM_DEFAULTS["alpine"]) == "PC-DATA first login: cisco / cisco"
    assert DL.console_hint("R1", fl, show=False) == "R1 first login: admin / •••••• (enable: ••••••)" + con
    login_con = DL.parse_day0("iosv", "username admin privilege 15 secret cisco\nenable secret cisco\n"
                                      "line con 0\n exec-timeout 0 0\n login local\nline vty 0 4\n login local\n")
    assert DL.console_hint("R1", login_con) == "R1 first login: admin / cisco (enable: cisco)"
    assert DL.console_hint("JR1", DL.PLATFORM_DEFAULTS["vjunos"]) == "JR1 first login: root / no password"
    blank = DL.first_login("iosv", "hostname R9\n")
    assert DL.console_hint("R9", blank) == "R9 first login: none — press Enter (enable: no password)"
    assert DL.console_hint("SW", None) == ""


def test_credentials_dialog_reset_button_and_inspector_summary(cfile):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from labdesk_app.ui.dialogs import CredentialsDialog
    from labdesk_app.ui.inspector import login_summary
    first = DL.PLATFORM_DEFAULTS["iosv"]
    C.save("R1", {"USERNAME": "me", "PASSWORD": "mine", "ENABLE": "mine"})
    dlg = CredentialsDialog("R1", C.load_detail("R1"), None, first_login=first)
    assert dlg.reset_btn.text() == "Reset to default" and not dlg.reset_requested
    dlg.reset_btn.click()
    assert dlg.reset_requested and dlg.result() == CredentialsDialog.Accepted
    assert login_summary("R1", first) == "me · password + enable secret (saved in LabDesk)"
    C.reset_to_default("R1", first.creds_values())
    assert C.load("R1").password == "cisco"
    assert login_summary("R1", first) == "admin · password + enable secret (LabDesk will try this default, not read from device)"
    assert login_summary("R7", DL.first_login("iosv", "hostname R7\n")) == \
        "no login: press Enter · enable: no password (default, not read from device)"
    assert not hasattr(CredentialsDialog("R2", C.load_detail("R2"), None), "reset_btn")   # no default known
    app.processEvents()
