"""Preparation must reuse the qualified immutable prior, including old manifests."""
import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import pytest
from pathlib import Path

from deploy import coordinated as c
from deploy.coordinated_runtime import HELPER, helper_release_root
from .test_deploy_coordinated_helpers import runtime_preparation


@pytest.fixture
def legacy_prior(runtime_preparation):
    runtime, fixture, intent, root = runtime_preparation
    policy = c.read_json(fixture.policy)
    bundle = root / "legacy-prior-bundle"
    bundle.mkdir(mode=0o700)
    pin = c.read_json(fixture.b.config)["pin"]
    manifest = runtime.installer._archive(fixture.repo, fixture.prior, bundle, provider_cli_pin=pin)
    # The historical installer did not add the test_settings field or overlay.
    manifest.pop("test_settings")
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    target = runtime.installer.Target("fixture", "localhost",
        str(helper_release_root(policy["executors"]["fixture"]["script"])), True)
    release = Path(runtime.installer.stage_helper_source(target, bundle, fixture.prior, manifest))
    policy["executors"]["fixture"]["script"] = str(release / "app" / HELPER)
    c.atomic_json(fixture.policy, policy)
    before = {name: (release / name).read_bytes() for name in ("manifest.json", "pentacle.tar")}
    overlay = fixture.repo / "services/_shared/spawn_defaults.local.json"
    c.atomic_json(overlay, {"schema_version": 1})
    with (fixture.repo / ".git/info/exclude").open("a") as handle:
        handle.write("services/_shared/spawn_defaults.local.json\n")
    qualified = c.seal_receipt({"schema": "pentacle.coordinated-deploy.receipt.v1",
        "intent": {"target_sha": fixture.prior}, "source": {"target": {"manifest": manifest}}})
    witness = root / "qualified-prior.json"
    c.atomic_json(witness, qualified)
    stamp = runtime.d._read_stamp(fixture.repo, runtime.service)
    stamp.update(coordinated_receipt=str(witness), receipt_sha256=qualified["receipt_sha256"])
    runtime.d._write_stamp(fixture.repo, runtime.service, stamp)
    return SimpleNamespace(runtime=runtime, fixture=fixture, intent=intent, root=root,
        manifest=manifest, release=release, overlay=overlay, before=before)


def test_prepare_reuses_real_legacy_prior_without_restamping(legacy_prior):
    case = legacy_prior
    runtime, fixture, intent, root = case.runtime, case.fixture, case.intent, case.root
    manifest, release, overlay, before = case.manifest, case.release, case.overlay, case.before
    folder = root / "reuse-prior"
    folder.mkdir(mode=0o700)

    prepared = runtime.prepare(intent, folder)

    assert prepared["source"]["prior"]["manifest"] == manifest
    assert "test_settings" not in prepared["source"]["prior"]["manifest"]
    assert prepared["source"]["prior"]["helpers"]["fixture"]["script"] == str(release / "app" / HELPER)
    assert prepared["preimages"]["test_settings"]["sha256"] == c.file_digest(overlay)
    assert Path(prepared["preimages"]["test_settings"]["saved"]).read_bytes() == overlay.read_bytes()
    assert prepared["source"]["target"]["smoke_plan"]["test_profile"]
    assert not (release / "app/services/_shared/spawn_defaults.local.json").exists()
    assert before == {name: (release / name).read_bytes() for name in before}
    runtime.guard = lambda *a, **k: {"sole_deployer": True}
    receipt = {"intent": intent, "transaction_directory": str(folder), **prepared}
    runtime.revalidate(receipt, forward=True)
    assert fixture.git("rev-parse", "HEAD") == fixture.prior
    assert not runtime.stopped


@pytest.mark.parametrize("damage", ["archive", "source", "qualified_manifest"])
def test_installed_prior_reuse_keeps_existing_integrity_guards(legacy_prior, damage):
    case = legacy_prior
    if damage == "archive":
        with (case.release / "pentacle.tar").open("ab") as handle:
            handle.write(b"corruption")
    elif damage == "source":
        with (case.release / "app/services/_shared/spawn_profiles.py").open("a") as handle:
            handle.write("\n# corrupted installed source\n")
    else:
        manifest = c.read_json(case.release / "manifest.json")
        manifest["stamp"] = "unqualified-restamp"
        c.atomic_json(case.release / "manifest.json", manifest)
    folder = case.root / "refuse-corrupt-prior"
    folder.mkdir(mode=0o700)
    with pytest.raises((c.Invalid, subprocess.CalledProcessError)):
        case.runtime.prepare(case.intent, folder)
    assert case.fixture.git("rev-parse", "HEAD") == case.fixture.prior
    assert not case.runtime.stopped


def test_reuse_verifies_git_source_independently_of_self_consistent_archive(legacy_prior):
    case = legacy_prior
    # Legacy direct qualification can be stamp-only; it still cannot admit a
    # coherently rehashed replacement for a tracked prior source file.
    stamp = case.runtime.d._read_stamp(case.fixture.repo, case.runtime.service)
    stamp.pop("coordinated_receipt"); stamp.pop("receipt_sha256")
    case.runtime.d._write_stamp(case.fixture.repo, case.runtime.service, stamp)
    name = "services/_shared/asset_schema.py"
    original = case.release / "pentacle.tar"
    replacement = case.root / "corrupt-prior.tar"
    with tarfile.open(original) as archive, tarfile.open(replacement, "w") as output:
        for member in archive:
            data = archive.extractfile(member).read() if member.isfile() else None
            if member.name == name:
                data += b"\n# modified tracked source\n"
                member.size = len(data)
                (case.release / "app" / name).write_bytes(data)
            output.addfile(member, io.BytesIO(data) if data is not None else None)
    original.write_bytes(replacement.read_bytes())
    manifest = c.read_json(case.release / "manifest.json")
    manifest["archive_sha256"] = c.file_digest(original)
    c.atomic_json(case.release / "manifest.json", manifest)
    target = case.runtime.installer.Target("fixture", "localhost", str(case.release.parent), True)
    case.runtime.installer.verify_helper_source(target, case.fixture.prior, manifest)
    folder = case.root / "refuse-rehashed-prior"
    folder.mkdir(mode=0o700)
    with pytest.raises(c.Invalid, match="differs from Git source"):
        case.runtime.prepare(case.intent, folder)
    assert not case.runtime.stopped
