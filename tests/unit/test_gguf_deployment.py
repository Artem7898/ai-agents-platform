"""Native key-file auth must fail closed before the inference server starts."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).parents[2] / "deploy/gguf/preflight.py"
_SPEC = importlib.util.spec_from_file_location("gguf_preflight", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)

KEY = "sensitive-test-key-DO-NOT-LOG-1234567890abcdefgh"


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n# no configured keys\n",
        b"too-short\n",
        (KEY + "\r\n").encode(),
        (KEY + " \n").encode(),
        b"a" * 31 + b"\x00\n",
        b"a" * 31 + b"\xff\n",
        b"a" * 513,
        (KEY + "\n" + KEY + "\n").encode(),
        "\n".join(str(i) * 48 for i in range(5)).encode(),
        b"#" * 16_385,
    ],
)
def test_invalid_native_key_files_never_authorize_startup(tmp_path, content):
    path = tmp_path / "keys"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="1 to 4 distinct printable keys") as error:
        preflight.validate_api_key_file(str(path))
    assert KEY not in str(error.value)


def test_native_key_file_accepts_a_rotation_overlap_without_returning_keys(tmp_path):
    path = tmp_path / "keys"
    path.write_text(f"# old and new keys\n{KEY}\n\n{'b' * 48}\n")
    assert preflight.validate_api_key_file(str(path)) == 2


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "missing"])
def test_native_key_file_cannot_redirect_or_block_preflight(tmp_path, kind):
    path = tmp_path / "keys"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text(KEY)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    with pytest.raises(ValueError):
        preflight.validate_api_key_file(str(path))


@pytest.fixture
def configured_preflight(tmp_path, monkeypatch):
    receipt = tmp_path / "verification"
    receipt.mkdir()
    key = tmp_path / "keys"
    key.write_text(KEY + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"placeholder":"test manifest"}')
    monkeypatch.setattr(preflight, "_KEY_FILE", str(key))
    monkeypatch.setattr(preflight, "_MANIFEST_FILE", manifest)
    monkeypatch.setattr(preflight, "_RECEIPT_DIRECTORY", receipt)
    monkeypatch.setenv("GGUF_MODEL_ALIAS", "pilot-v1")
    monkeypatch.setenv("GGUF_CONTEXT_SIZE", "4096")
    report = {
        "sha256": "a" * 64,
        "size_bytes": 64,
        "model_alias": "pilot-v1",
        "context_size": 4096,
        "manifest_file_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "manifest_file_size": manifest.stat().st_size,
    }
    return receipt, key, manifest, report


def test_receipt_is_published_only_after_successful_independent_verification(
    configured_preflight, monkeypatch, capsys
):
    receipt, _, manifest, report = configured_preflight
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(report)),
    )
    assert preflight.main() == 0
    assert (receipt / "model.sha256").read_text() == "a" * 64 + "\n"
    assert (receipt / "model.alias").read_text() == "pilot-v1\n"
    assert (receipt / "context-size").read_text() == "4096\n"
    assert (receipt / "model.size").read_text() == "64\n"
    assert (receipt / "manifest.size").read_text().strip() == str(manifest.stat().st_size)
    assert (receipt / "manifest.sha256").read_text().strip() == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert KEY not in capsys.readouterr().out


def test_a_failed_reverification_removes_previous_startup_authorization(
    configured_preflight, monkeypatch
):
    receipt, _, _, _ = configured_preflight
    for name in preflight._RECEIPT_FILES:
        (receipt / name).write_text("previously authorized\n")
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout=""),
    )
    assert preflight.main() == 2
    assert list(receipt.iterdir()) == []


def test_receipt_keeps_the_verified_manifest_hash_when_the_host_file_changes(
    configured_preflight, monkeypatch
):
    receipt, _, manifest, report = configured_preflight

    def change_manifest(*args, **kwargs):
        manifest.write_text("changed while verifying")
        return SimpleNamespace(returncode=0, stdout=json.dumps(report))

    monkeypatch.setattr(preflight.subprocess, "run", change_manifest)
    assert preflight.main() == 0
    # The startup wrapper rejects the changed host bytes against this original digest.
    assert (receipt / "manifest.sha256").read_text().strip() == report["manifest_file_sha256"]
    assert (receipt / "manifest.sha256").read_text().strip() != hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
