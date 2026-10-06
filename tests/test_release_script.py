"""Regression tests for release-owned migration script selection."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_release():
    path = ROOT / "deploy/sf_provider_migration/release.py"
    spec = importlib.util.spec_from_file_location("test_release_script_module", path)
    module = importlib.util.module_from_spec(spec)
    with patch("os.chdir"), patch("os.umask"):
        spec.loader.exec_module(module)
    return module


def test_default_metadata_script_remains_shipping_script():
    module = load_release()
    with patch.dict(os.environ, {}, clear=True):
        assert module.metadata_script_relative() is None
        assert module.metadata_container_path() == "/run/release/metadata.py"
        assert module.metadata_script_path() == ROOT / "deploy/sf_provider_migration/metadata.py"


def test_build_base_image_defaults_to_running_base_and_allows_explicit_rebase():
    module = load_release()
    with patch.dict(os.environ, {"BASE_IMAGE": "running"}, clear=True):
        assert module.build_base_image() == "running"
    with patch.dict(os.environ, {"BASE_IMAGE": "running", "BUILD_BASE_IMAGE": "low-layers"}, clear=True):
        assert module.build_base_image() == "low-layers"


def test_metadata_script_rejects_non_flow_or_traversal_paths():
    module = load_release()
    for value in (
        "/tmp/metadata.py",
        "metadata.py",
        "app-source/erpnext/metadata.py",
        "app-source/flow/../erpnext/metadata.py",
        "app-source/flow/metadata.txt",
    ):
        with patch.dict(os.environ, {"METADATA_SCRIPT_RELATIVE": value}, clear=True):
            try:
                module.metadata_script_relative()
            except AssertionError:
                pass
            else:
                raise AssertionError(value)


def test_custom_flow_script_requires_exact_checkout_and_archive(monkeypatch):
    module = load_release()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checkout = root / "git-source/flow/deploy/customer_service_workflows/metadata.py"
        archive = root / "app-source/flow/deploy/customer_service_workflows/metadata.py"
        checkout.parent.mkdir(parents=True)
        archive.parent.mkdir(parents=True)
        checkout.write_text("print('flow')\n")
        archive.write_bytes(checkout.read_bytes())
        monkeypatch.setattr(module, "ROOT", root)
        monkeypatch.setattr(module, "required", lambda key: "a" * 40)
        with patch.dict(
            os.environ,
            {"METADATA_SCRIPT_RELATIVE": "app-source/flow/deploy/customer_service_workflows/metadata.py"},
            clear=True,
        ):
            def git_output(command, **kwargs):
                if "ls-tree" in command:
                    return "100644 blob abc123 deploy/customer_service_workflows/metadata.py"
                assert "hash-object" in command
                return "abc123"

            with patch.object(module, "run", side_effect=git_output):
                assert module.metadata_script_path(verify_flow_archive=True) == archive
                assert module.metadata_container_path() == "/run/release/app-source/flow/deploy/customer_service_workflows/metadata.py"
                assert module.metadata_script_descriptor()["sha256"] == module.sha(archive)

            archive.write_text("print('changed')\n")
            try:
                module.metadata_script_path(verify_flow_archive=True)
            except AssertionError as error:
                assert "flow_metadata_archive_mismatch" in str(error)
            else:
                raise AssertionError("modified source archive was accepted")


def test_prepare_metadata_permissions_only_exposes_selected_file(monkeypatch):
    module = load_release()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        selected = root / "app-source/flow/deploy/customer_service_workflows/metadata.py"
        selected.parent.mkdir(parents=True)
        selected.write_text("flow\n")
        selected.chmod(0o600)
        secret = root / "secrets"
        secret.mkdir(mode=0o700)
        (secret / "secret").write_text("private")
        (secret / "secret").chmod(0o600)
        monkeypatch.setattr(module, "ROOT", root)
        monkeypatch.setattr(module, "metadata_script_path", lambda **_: selected)
        module.prepare_metadata_permissions()
        assert stat.S_IMODE(selected.stat().st_mode) & 0o004
        for parent in (selected.parent, selected.parent.parent, selected.parent.parent.parent, root):
            assert stat.S_IMODE(parent.stat().st_mode) & 0o001
        assert stat.S_IMODE(secret.stat().st_mode) == 0o700
        assert stat.S_IMODE((secret / "secret").stat().st_mode) == 0o600


def test_custom_flow_migration_always_requires_failure_injection():
    module = load_release()
    actions = {"already_migrated": True}
    with patch.dict(os.environ, {"METADATA_SCRIPT_RELATIVE": "app-source/flow/deploy/customer_service_workflows/metadata.py"}, clear=True):
        assert module.failure_injection_required(actions)
        assert module.failure_injection_marker() == b"ISOLATED_INJECTED_FAILURE_AFTER_OWNER:FLOW_METADATA"
    with patch.dict(os.environ, {}, clear=True):
        assert not module.failure_injection_required(actions)
        assert module.failure_injection_required({"already_migrated": False})
        assert module.failure_injection_marker() == b"ISOLATED_INJECTED_FAILURE_AFTER_OWNER"


def test_rehearsal_preflight_failure_creates_no_isolation_resources(monkeypatch, tmp_path):
    module = load_release()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "state", lambda: {})
    with patch.object(module, "metadata_read_preflight", side_effect=AssertionError("unreadable")), patch.object(module, "run") as run:
        with pytest.raises(AssertionError, match="unreadable"):
            module.rehearse()
    run.assert_not_called()
    assert not (tmp_path / "isolation").exists()


def test_metadata_read_preflight_uses_app_user_and_both_images(monkeypatch, tmp_path):
    module = load_release()
    script = tmp_path / "metadata.py"
    script.write_text("flow\n")
    monkeypatch.setattr(module, "metadata_script_path", lambda **_: script)
    with patch.dict(os.environ, {"BASE_IMAGE": "baseline", "NEW_IMAGE": "candidate"}, clear=True), patch.object(module, "run", return_value=module.sha(script)) as run:
        module.metadata_read_preflight()
    assert run.call_count == 2
    for call, image in zip(run.call_args_list, ("baseline", "candidate")):
        command = call.args[0]
        assert command[command.index("--user") + 1] == "frappe"
        assert command[command.index("--network") + 1] == "none"
        assert image in command
        assert command[-1] == "/run/release/metadata.py"
        assert any(value.endswith("target=/run/release,readonly") for value in command)


@pytest.mark.parametrize("overrides", [
    {},
    {"BASE_IMAGE": "verified-current-image", "BASE_FLOW_REV": "f" * 40, "SHIPPING_REV": "s" * 40},
    {"BASE_IMAGE": ""},
    {"BASE_IMAGE": "literal $(touch unexpected-command) `touch unexpected-backticks`; spaces"},
])
def test_bash_entrypoint_preserves_explicit_environment_over_defaults(tmp_path, overrides):
    release = tmp_path / "release"
    release.mkdir()
    source = ROOT / "deploy/sf_provider_migration"
    shutil.copyfile(source / "release.sh", release / "release.sh")
    shutil.copyfile(source / "release.env", release / "release.env")
    # Exercise the real shell entry point without loading deployment code.
    (release / "release.py").write_text(
        "import json, os, pathlib, sys\n"
        "keys = ['BASE_IMAGE', 'BASE_FLOW_REV', 'SHIPPING_REV', 'PROJECT', 'SITE']\n"
        "pathlib.Path(os.environ['RELEASE_TEST_CAPTURE']).write_text(json.dumps({"
        "'arguments': sys.argv[1:], 'environment': {key: os.environ.get(key) for key in keys}}))\n"
    )
    output = tmp_path / "environment.json"
    environment = {"PATH": os.environ["PATH"], "RELEASE_TEST_CAPTURE": str(output), "RELEASE_NO_TEE": "1", **overrides}
    subprocess.run(["bash", str(release / "release.sh"), "prepare"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, check=True, timeout=10)
    captured = json.loads(output.read_text())
    assert captured["arguments"] == ["prepare"]
    values = captured["environment"]
    assert values["PROJECT"] == "leya-erpnext-v16"
    assert values["SITE"] == "erp-sunny.leyabilliards.com"
    for key, value in overrides.items():
        assert values[key] == value
    if not overrides:
        assert values["BASE_IMAGE"] == "leya/erpnext:v16.36.0-shipment-carrier-isolation-20260929-r9"
        assert values["BASE_FLOW_REV"] == "9af143dfdfc33a6252208a5a8b5f2274fbadd8ef"
    assert not (release / "unexpected-command").exists()
    assert not (release / "unexpected-backticks").exists()
