"""Downloading the LabDesk-VMs bundle from a GitHub release (vmfetch) and publishing it (publish_vm_bundle):
parts, resume, checksums, the bundle appearing only when complete.  GitHub is replaced by a fake."""
from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

from labdesk_app import vmfetch, vmware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


def make_release(tmp_path, part_size=10):
    """A bundle on disk -> (manifest dict, {asset name: bytes}) exactly as publish_vm_bundle cuts it."""
    import publish_vm_bundle as pub
    root = tmp_path / "src"
    (root / "CML").mkdir(parents=True)
    (root / "CML" / "cml.vmdk").write_bytes(bytes(range(256)) * 3 + b"tail")     # 772 bytes -> 78 parts
    (root / "CML" / "CML.vmx").write_bytes(b'displayName = "x"\n')
    (root / "cml-credentials.env").write_text("CML_ADMIN_PASS=secret\n")           # never published
    bundle = {"format": 1, "cml": {"vmx": "CML/CML.vmx"}, "templates": {}}
    (root / vmware.MANIFEST).write_text(json.dumps(bundle))
    files = pub.files_of(root)
    assert sorted(f.name for f in files) == ["CML.vmx", "cml.vmdk"]
    entries = [pub.describe(root, f, part_size) for f in files]
    assets = {}
    for e in entries:
        data = (root / e["path"]).read_bytes()
        for i, p in enumerate(e["parts"]):
            assets[p["name"]] = data[i * part_size:(i + 1) * part_size]
            assert hashlib.sha256(assets[p["name"]]).hexdigest() == p["sha256"]
    manifest = {"format": vmfetch.FORMAT, "tag": "vms-1", "part_size": part_size,
                "total": sum(e["size"] for e in entries), "bundle": bundle, "files": entries}
    return manifest, assets


class FakeGitHub:
    def __init__(self, manifest, assets, fail_after=None):
        self.assets = dict(assets)
        self.assets[vmfetch.MANIFEST_ASSET] = json.dumps(manifest).encode()
        self.ids = {n: i for i, n in enumerate(self.assets, 1)}
        self.by_id = {i: n for n, i in self.ids.items()}
        self.fail_after = fail_after
        self.served = 0
        self.ranges = []

    def json(self, path, token):
        assert token == "tok"
        return [{"tag_name": "vms-1", "assets": [{"name": n, "id": i} for n, i in self.ids.items()]}]

    def response(self, asset_id, token, rng=0):
        name = self.by_id[asset_id]
        if self.fail_after is not None and self.served >= self.fail_after and name != vmfetch.MANIFEST_ASSET:
            raise OSError("connection reset")
        self.served += 1
        self.ranges.append((name, rng))
        r = io.BytesIO(self.assets[name][rng:])
        r.status = 206 if rng else 200
        return r


@pytest.fixture
def gh(tmp_path, monkeypatch):
    manifest, assets = make_release(tmp_path)
    fake = FakeGitHub(manifest, assets)
    monkeypatch.setattr(vmfetch, "_json", fake.json)
    monkeypatch.setattr(vmfetch, "_asset_response", fake.response)
    monkeypatch.setattr(vmfetch.time, "sleep", lambda s: None)
    return fake


def test_part_names():
    assert vmfetch.part_name("CML/cml.vmdk", 1) == "CML--cml.vmdk.p01"
    assert vmfetch.part_name("templates/vjunos/vjunos.vmdk", 12) == "templates--vjunos--vjunos.vmdk.p12"


def test_find_remote_reads_the_manifest(gh):
    r = vmfetch.find_remote("tok")
    assert r.tag == "vms-1" and r.total == 772 + len('displayName = "x"\n')


def test_fetch_builds_the_bundle(gh, tmp_path):
    dest = tmp_path / "LabDesk-VMs"
    root = vmfetch.fetch(log=lambda m: None, token="tok", dest=dest)
    assert (root / "CML" / "cml.vmdk").read_bytes() == bytes(range(256)) * 3 + b"tail"
    assert (root / "CML" / "CML.vmx").read_bytes() == b'displayName = "x"\n'
    assert json.loads((root / vmware.MANIFEST).read_text())["cml"]["vmx"] == "CML/CML.vmx"
    assert not (root / ".download").exists() and not (root / "cml-credentials.env").exists()


def test_fetch_resumes_after_a_break(gh, tmp_path):
    dest = tmp_path / "LabDesk-VMs"
    gh.fail_after = 30                                # the line drops after 30 assets (5 retries fail too)
    with pytest.raises(vmfetch.FetchError):
        vmfetch.fetch(log=lambda m: None, token="tok", dest=dest)
    assert not (dest / vmware.MANIFEST).exists()      # incomplete: find_bundle must not see it
    gh.fail_after = None
    gh.ranges.clear()
    root = vmfetch.fetch(log=lambda m: None, token="tok", dest=dest)
    assert (root / "CML" / "cml.vmdk").read_bytes() == bytes(range(256)) * 3 + b"tail"
    fetched = [n for n, _r in gh.ranges if n != vmfetch.MANIFEST_ASSET]
    total = len([n for n in gh.assets if n.startswith("CML--cml.vmdk")])
    assert fetched[0] != vmfetch.part_name("CML/cml.vmdk", 1)       # resumed, not from the start
    assert len(fetched) <= total - 25                 # the parts already in place were not downloaded again


def test_corrupt_part_is_downloaded_again(gh, tmp_path):
    dest = tmp_path / "LabDesk-VMs"
    name = vmfetch.part_name("CML/cml.vmdk", 3)
    good = gh.assets[name]
    calls = {"n": 0}
    orig = gh.response

    def flaky(asset_id, token, rng=0):
        if gh.by_id[asset_id] == name and calls["n"] == 0:
            calls["n"] += 1
            r = io.BytesIO(b"X" * len(good))
            r.status = 200
            return r
        return orig(asset_id, token, rng)
    gh.response = flaky
    import labdesk_app.vmfetch as vf
    vf._asset_response = flaky
    root = vmfetch.fetch(log=lambda m: None, token="tok", dest=dest)
    assert (root / "CML" / "cml.vmdk").read_bytes() == bytes(range(256)) * 3 + b"tail"


def test_not_enough_disk_space(gh, tmp_path, monkeypatch):
    monkeypatch.setattr(vmfetch.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(vmfetch.FetchError, match="not enough disk space"):
        vmfetch.fetch(log=lambda m: None, token="tok", dest=tmp_path / "x")


def test_incomplete_release_is_refused(tmp_path, monkeypatch):
    manifest, assets = make_release(tmp_path)
    assets.pop(next(iter(assets)))
    fake = FakeGitHub(manifest, assets)
    monkeypatch.setattr(vmfetch, "_json", fake.json)
    monkeypatch.setattr(vmfetch, "_asset_response", fake.response)
    with pytest.raises(vmfetch.FetchError, match="incomplete"):
        vmfetch.find_remote("tok")


def test_token_from_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "abc")
    assert vmfetch.github_token() == "abc"


def test_repo_name_is_validated(monkeypatch):
    monkeypatch.setenv("LABDESK_VMS_REPO", "bad repo; rm -rf")
    with pytest.raises(vmfetch.FetchError):
        vmfetch.repo()
