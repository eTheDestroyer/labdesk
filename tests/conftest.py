import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# L1: the test run never reads or writes the user's ~/.config/LabDesk
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="labdesk-tests-")


# LabDesk for Windows: tests of Linux-only machinery (pty / Konsole / askpass / file modes / bash helpers)
# carry @pytest.mark.linux_only and are skipped on Windows; everything else runs on both.
def pytest_configure(config):
    config.addinivalue_line("markers", "linux_only: needs Linux (pty, Konsole, POSIX file modes, bash)")


def pytest_collection_modifyitems(config, items):
    if sys.platform != "win32":
        return
    import pytest
    skip = pytest.mark.skip(reason="Linux only (LabDesk for Windows uses other machinery)")
    for item in items:
        if "linux_only" in item.keywords:
            item.add_marker(skip)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_vm_bundle(monkeypatch):
    """Never find the user's real LabDesk-VMs bundle (Windows/VMware tests bring their own)."""
    from labdesk_app import vmware
    monkeypatch.setattr(vmware, "bundle_candidates", lambda: [])
    monkeypatch.setattr(vmware, "_BUNDLE", None)
    yield
