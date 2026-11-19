"""CML connection dialog: address, username, password (+ *Test connection*), saved to the credentials file.

Shown on first run when the credentials file is missing or has no CML_ADMIN_PASS (on Windows there is no
install.sh), and from the *Lab* menu ▸ *CML connection…* at any time.  The password is written only to
the credentials file (owner-only), never to LabDesk.ini, the event log or a command line; a saved
password is never shown - leave the field empty to keep it.

Windows also asks for the CML VM's VMware ``.vmx`` file (optional): *Start CML VM* then runs
``vmrun start <vmx>``.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QWidget)

from .. import config, plat, validate
from . import theme as T
from .dialogs import LabDeskDialog, secret_field
from .worker import TaskRunner

DEFAULT_HOST = "" if plat.IS_WINDOWS else "192.168.122.60"
VMX_KEY = "cml/vmx_path"


def _default_client(host: str):
    from ..cml_api import CMLClient
    return CMLClient(host=host, timeout=8.0)


class CmlConnectionDialog(LabDeskDialog):
    """``saved`` is True after *Save*; ``host`` / ``user`` hold what was saved."""

    def __init__(self, parent=None, *, cred_file: Path | None = None, first_run: bool = False,
                 client_factory=None, settings=None):
        sub = ("Tell LabDesk where your Cisco Modeling Labs controller is. You only do this once."
               if first_run else "The CML controller LabDesk talks to.")
        super().__init__("CML connection", parent, header=("network-connect",
                                                             "Welcome to LabDesk" if first_run else "CML connection",
                                                             sub))
        self.setObjectName("cmlConnectionDialog")
        self.cred_file = Path(cred_file or config.CRED_FILE)
        self._client_factory = client_factory or _default_client
        self._settings = settings
        self._runner = TaskRunner(self, max_threads=1)
        self.saved = False
        try:
            cur = config.load_env_file(self.cred_file)
        except OSError:
            cur = {}
        self._has_password = bool(cur.get("CML_ADMIN_PASS"))
        self.setMinimumWidth(520)
        self._touched = False

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.host = QLineEdit(cur.get("CML_HOST") or (config.CML_HOST if self.cred_file.exists() else DEFAULT_HOST))
        self.host.setPlaceholderText("e.g. 192.168.80.130")
        self.host.setFont(T.mono())
        self.host.setToolTip("The IP address (or name) you open in the browser to reach CML: https://<address>")
        self.user = QLineEdit(cur.get("CML_ADMIN_USER") or "admin")
        self.user.setFont(T.mono())
        self.password = secret_field(placeholder="saved - leave empty to keep it" if self._has_password
                                     else "set during CML setup")
        form.addRow("CML address", self.host)
        form.addRow("Username", self.user)
        form.addRow("Password", self.password)
        self.vmx = None
        bundle = None
        if plat.IS_WINDOWS:
            from .. import vmware
            bundle = vmware.find_bundle()
        self.sysadmin = None
        if bundle is not None:
            self._has_sys = bool(cur.get("CML_SYSADMIN_PASS"))
            self.sysadmin = secret_field(placeholder="saved - leave empty to keep it" if self._has_sys
                                         else "CML sysadmin password (Internet cloud)")
            self.sysadmin.setToolTip("The CML 'sysadmin' password (Cockpit, port 9090): LabDesk reads and sets "
                                     "the Internet cloud inside the CML VM with it")
            form.addRow("Sysadmin password", self.sysadmin)
            if not self.host.text():
                self.host.setText(bundle.manifest.get("cml", {}).get("ip") or "192.168.122.60")
            note = QLabel(f"CML runs on this PC in VMware Workstation, from {bundle.root}. LabDesk starts it "
                          "by itself; the login above came with that folder.")
            note.setWordWrap(True)
            note.setObjectName("muted")
            note.setFont(T.type("small"))
            form.addRow("CML VM", note)
        elif plat.IS_WINDOWS:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            self.vmx = QLineEdit(str(self._setting(VMX_KEY, "") or ""))
            self.vmx.setPlaceholderText("optional: CML's .vmx (VMware)")
            self.vmx.setToolTip("VMware Workstation file of the CML virtual machine. When set, "
                                "'Start CML VM' starts it for you (vmrun).")
            browse = QPushButton("Browse…")
            browse.clicked.connect(self._browse_vmx)
            h.addWidget(self.vmx, 1)
            h.addWidget(browse)
            form.addRow("CML VM file", row)
        self.body.addLayout(form)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.setObjectName("testConnection")
        self.test_btn.clicked.connect(self.test_connection)
        test_row.addWidget(self.test_btn)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setFont(T.type("small"))
        test_row.addWidget(self.status, 1)
        self.body.addLayout(test_row)

        where = str(self.cred_file)
        hint = QLabel(f"Saved only on this computer, in {where} (readable by your user account only). "
                      "The password is never shown again, logged or put on a command line.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        hint.setFont(T.type("small"))
        self.body.addWidget(hint)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {T.status_text('down').name()};")
        self.error.setFont(T.type("small"))
        self.body.addWidget(self.error)

        self.cancel_button("Later" if first_run else "Cancel")
        self.ok = self.add_button("Save", QDialogButtonBox.AcceptRole, "primary", default=True)
        try:
            self.buttons.accepted.disconnect(self.accept)
        except (RuntimeError, TypeError):
            pass
        self.buttons.accepted.connect(self._save)
        for e in (self.host, self.user, self.password):
            e.textChanged.connect(self._check)
            e.textEdited.connect(self._touch)
        self._check()
        (self.password if self.host.text() and not self._has_password else self.host).setFocus()

    # ----------------------------------------------------------------- helpers
    def _setting(self, key, default=None):
        st = self._settings
        if st is None:
            from ..settings import app_settings
            st = self._settings = app_settings()
        return st.value(key, default)

    def _browse_vmx(self):
        start = self.vmx.text() or str(Path.home() / "Documents" / "Virtual Machines")
        path, _ = QFileDialog.getOpenFileName(self, "CML virtual machine", start, "VMware VM (*.vmx)")
        if path:
            self.vmx.setText(path)

    def values(self) -> tuple[str, str, str]:
        return self.host.text().strip(), self.user.text().strip(), self.password.text()

    def problem(self) -> str:
        host, user, pw = self.values()
        if not host:
            return "Enter the CML address."
        try:
            validate.host(host)
        except validate.ValidationError:
            return "The CML address must be an IP address or host name (no https://, no path)."
        try:
            validate.username(user)
        except validate.ValidationError:
            return "Enter a valid username (usually admin)."
        if not pw and not self._has_password:
            return "Enter the CML password."
        if "\n" in pw or "\r" in pw:
            return "The password cannot contain line breaks."
        return ""

    def _touch(self, *_):
        self._touched = True
        self._check()

    def _check(self, *_):
        msg = self.problem()
        self.ok.setEnabled(not msg)
        self.test_btn.setEnabled(not msg and self.test_btn.text() == "Test connection")
        self.error.setText(msg if self._touched else "")

    def _password_for_test(self) -> str:
        pw = self.password.text()
        if pw or not self._has_password:
            return pw
        try:
            return config.load_env_file(self.cred_file).get("CML_ADMIN_PASS", "")
        except OSError:
            return ""

    # ----------------------------------------------------------------- actions
    def test_connection(self):
        if self.problem():
            return
        host, user, _ = self.values()
        pw = self._password_for_test()
        self.test_btn.setText("Testing…")
        self.test_btn.setEnabled(False)
        self._set_status("Connecting to " + host + "…", "neutral")

        def work():
            self._client_factory(host).check_login(user, pw)
            return True

        def done(_):
            self.test_btn.setText("Test connection")
            self._check()
            self._set_status("✓ Connected - CML accepted the login.", "up")

        def fail(e):
            self.test_btn.setText("Test connection")
            self._check()
            from ..cml_api import CMLUnreachable
            if isinstance(e, CMLUnreachable):
                msg = (f"✕ No answer from {host}. Is the CML VM running, and can this computer reach it? "
                       f"(open https://{host} in a browser)")
            else:
                msg = f"✕ {e}"
            self._set_status(msg, "down")
        self._runner.submit(work, on_done=done, on_error=fail)

    def _set_status(self, text: str, state: str):
        self.status.setText(text)
        col = T.status_text(state).name() if state in ("up", "down", "warn") else ""
        self.status.setStyleSheet(f"color: {col};" if col else "")

    def _save(self):
        msg = self.problem()
        if msg:
            self.error.setText(msg)
            return
        host, user, pw = self.values()
        updates = {"CML_HOST": host, "CML_ADMIN_USER": user}
        if pw:
            updates["CML_ADMIN_PASS"] = pw
        if self.sysadmin is not None and self.sysadmin.text():
            updates["CML_SYSADMIN_PASS"] = self.sysadmin.text()
            updates.setdefault("CML_SYSADMIN_USER", "sysadmin")
        try:
            config.update_env_file(updates, self.cred_file)
        except OSError as e:
            self.error.setText(f"Could not save {self.cred_file}: {e.strerror or e}")
            return
        if self.vmx is not None:
            st = self._settings if self._settings is not None else None
            if st is None:
                self._setting(VMX_KEY)
                st = self._settings
            st.setValue(VMX_KEY, self.vmx.text().strip())
            st.sync()
        if Path(self.cred_file) == Path(config.CRED_FILE):
            config.reload_cml_host()
        self.saved = True
        self.saved_host, self.saved_user = host, user
        self.accept()

    def done(self, r):                       # noqa: D401  (QDialog.done: wait for a running test)
        self._runner.wait(10000)
        super().done(r)
