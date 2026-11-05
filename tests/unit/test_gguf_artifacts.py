"""Artifact declarations cannot redirect startup or bypass file verification."""

import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.gguf import artifacts
from src.gguf.artifacts import ArtifactValidationError, load_manifest, verify_artifact


def model_bytes(*, version=3, tensors=1, metadata=1, payload=b"fixture-tensor-data"):
    # This fixture is a header/checksum artifact, not usable inference weights.
    return b"GGUF" + struct.pack("<IQQ", version, tensors, metadata) + payload


def declaration(data):
    return {
        "schema_version": 1,
        "model_alias": "qwen3-4b-ru-en-q4km-v1",
        "model_file": "model.gguf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "source": {
            "url": "https://huggingface.co/Qwen/Qwen3-4B-GGUF",
            "revision": "a" * 40,
        },
        "license": {
            "identifier": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
        "quantization": "Q4_K_M",
        "context_size": 4096,
        "chat_template": "embedded",
        "languages": ["ru", "en"],
    }


@pytest.fixture
def files(tmp_path):
    data = model_bytes()
    model = tmp_path / "model.gguf"
    model.write_bytes(data)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(declaration(data)))
    return manifest, model


def replace_field(path, field, value):
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("version", [2, 3])
def test_verified_report_identifies_header_check_scope_and_preserves_public_metadata(
    tmp_path, version
):
    data = model_bytes(version=version)
    model = tmp_path / "model.gguf"
    model.write_bytes(data)
    manifest = tmp_path / "manifest.json"
    definition = declaration(data)
    manifest.write_text(json.dumps(definition))
    report = verify_artifact(
        manifest, model, expected_alias=definition["model_alias"], context_size=4096
    )
    assert report["sha256"] == hashlib.sha256(data).hexdigest()
    assert report["gguf_version"] == version
    assert report["artifact_verified_locally"] is True
    assert report["weights_load_verified"] is False
    assert report["verification_scope"] == "sha256_size_header"
    assert report["languages"] == ["ru", "en"]
    assert report["source"] == definition["source"]


def test_verification_receipt_hashes_exact_raw_manifest_bytes_and_preserves_loader_api(files):
    manifest, model = files
    definition = json.loads(manifest.read_text())
    raw = b" \n" + json.dumps(definition, indent=4).encode() + b"\n\n"
    manifest.write_bytes(raw)
    assert load_manifest(manifest) == definition
    report = verify_artifact(manifest, model)
    assert report["manifest_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["manifest_file_size"] == len(raw)
    assert "manifest_file_sha256" not in load_manifest(manifest)


def test_verification_receipt_does_not_reread_manifest_after_parsing(files, monkeypatch):
    manifest, model = files
    raw = manifest.read_bytes()
    original = artifacts._regular_file

    @contextmanager
    def changed_after_read(path, label):
        with original(path, label) as stream:
            yield stream
        if label == "Manifest":
            changed = json.loads(raw)
            changed["license"]["identifier"] = "MIT"
            manifest.write_text(json.dumps(changed, indent=4))

    monkeypatch.setattr(artifacts, "_regular_file", changed_after_read)
    report = verify_artifact(manifest, model)
    assert report["manifest_file_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["manifest_file_size"] == len(raw)
    assert report["license"]["identifier"] == "Apache-2.0"
    assert report["manifest_file_sha256"] != hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_model_file_is_hashed_in_bounded_chunks(files, monkeypatch):
    manifest, model = files
    data = model_bytes(payload=b"x" * (3 * artifacts._CHUNK_BYTES))
    model.write_bytes(data)
    manifest.write_text(json.dumps(declaration(data)))
    original = os.fdopen
    sizes = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.stream.__exit__(*arguments)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            sizes.append(size)
            assert 0 <= size <= artifacts._CHUNK_BYTES
            return self.stream.read(size)

    monkeypatch.setattr(
        os, "fdopen", lambda *args, **kwargs: BoundedReader(original(*args, **kwargs))
    )
    assert verify_artifact(manifest, model)["size_bytes"] == len(data)
    assert len(sizes) >= 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("model_alias", "../../private"),
        ("model_alias", "model --host 0.0.0.0"),
        ("model_alias", "x" * 129),
        ("model_file", "../../outside.gguf"),
        ("model_file", "model-00001-of-00002.gguf"),
        ("sha256", "a" * 63),
        ("sha256", "A" * 64),
        ("size_bytes", True),
        ("size_bytes", 0),
        ("size_bytes", 1024**4),
        ("context_size", True),
        ("context_size", "4096"),
        ("context_size", 127),
        ("context_size", 131073),
        ("quantization", "Q4_K_M; run-command"),
        ("chat_template", "https://example.com/untrusted.jinja"),
        ("languages", []),
        ("languages", ["en", "EN"]),
        ("languages", ["ru", 1]),
        ("languages", ["en/private-value"]),
    ],
)
def test_invalid_or_executable_manifest_values_are_rejected(files, field, value):
    manifest, _ = files
    replace_field(manifest, field, value)
    with pytest.raises(ArtifactValidationError):
        load_manifest(manifest)


@pytest.mark.parametrize("field", ["extra_args", "api_key", "model_path", "server_image"])
def test_manifest_cannot_supply_new_runtime_options(files, field):
    manifest, _ = files
    value = json.loads(manifest.read_text())
    value[field] = "private-value"
    manifest.write_text(json.dumps(value))
    with pytest.raises(ArtifactValidationError) as error:
        load_manifest(manifest)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/model",
        "https://user:private-secret@example.com/model",
        "https://example.com/model?token=private-secret",
        "https://example.com/model#private-secret",
        "https://example.com\\private-secret",
        "https://example.com:0/model",
        "https://example.com:99999/model",
        "https://example.com/\nprivate-secret",
    ],
)
def test_source_and_license_references_do_not_carry_url_credentials(files, url):
    manifest, _ = files
    value = json.loads(manifest.read_text())
    for field in ("source", "license"):
        changed = copy.deepcopy(value)
        changed[field]["url"] = url
        manifest.write_text(json.dumps(changed))
        with pytest.raises(ArtifactValidationError) as error:
            load_manifest(manifest)
        assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("revision", ["main", "latest", "v1.0", "abcdef0", True, "a" * 41])
def test_mutable_or_invalid_source_revision_is_rejected(files, revision):
    manifest, _ = files
    value = json.loads(manifest.read_text())
    value["source"]["revision"] = revision
    manifest.write_text(json.dumps(value))
    with pytest.raises(ArtifactValidationError):
        load_manifest(manifest)


@pytest.mark.parametrize("raw", [b'{"sha256":1,"sha256":2}', b'{"value":NaN}', b"\xff", b"[]"])
def test_ambiguous_nonfinite_or_invalid_json_is_rejected(tmp_path, raw):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(raw)
    with pytest.raises(ArtifactValidationError):
        load_manifest(manifest)


def test_nested_duplicate_fields_are_rejected(files):
    manifest, _ = files
    manifest.write_text(manifest.read_text().replace('"url":', '"url":"private-secret", "url":', 1))
    with pytest.raises(ArtifactValidationError, match="duplicate") as error:
        load_manifest(manifest)
    assert "private-secret" not in str(error.value)


def test_manifest_size_is_bounded_before_json_parsing(files):
    manifest, _ = files
    manifest.write_bytes(b" " * (artifacts._MANIFEST_BYTES + 1))
    with pytest.raises(ArtifactValidationError, match="size limit"):
        load_manifest(manifest)


def test_changed_model_size_or_fingerprint_prevents_verification(files):
    manifest, model = files
    model.write_bytes(model.read_bytes() + b"x")
    with pytest.raises(ArtifactValidationError, match="size"):
        verify_artifact(manifest, model)
    changed = bytearray(model.read_bytes()[:-1])
    changed[-1] ^= 1
    model.write_bytes(changed)
    with pytest.raises(ArtifactValidationError, match="SHA256"):
        verify_artifact(manifest, model)


@pytest.mark.parametrize(
    "data", [b"not-a-gguf", model_bytes(version=1), model_bytes(tensors=0), model_bytes(metadata=0)]
)
def test_complete_checksum_does_not_override_an_invalid_gguf_header(tmp_path, data):
    manifest = tmp_path / "manifest.json"
    definition = declaration(data)
    definition["size_bytes"] = max(len(data), 24)
    manifest.write_text(json.dumps(definition))
    model = tmp_path / "model.gguf"
    model.write_bytes(data.ljust(24, b"\0"))
    with pytest.raises(ArtifactValidationError, match="header"):
        verify_artifact(manifest, model)


@pytest.mark.parametrize(
    "option,value",
    [("expected_alias", "different-v1"), ("context_size", 2048), ("context_size", True)],
)
def test_runtime_alias_and_context_must_match_the_verified_artifact(files, option, value):
    manifest, model = files
    with pytest.raises(ArtifactValidationError, match="does not match"):
        verify_artifact(manifest, model, **{option: value})


def test_explicit_model_path_must_use_the_fixed_filename(files):
    manifest, model = files
    other = model.with_name("another.gguf")
    model.rename(other)
    with pytest.raises(ArtifactValidationError, match="model.gguf"):
        verify_artifact(manifest, other)


@pytest.mark.parametrize("which", ["model", "manifest"])
def test_manifest_and_model_symlinks_are_rejected(files, which):
    manifest, model = files
    selected = model if which == "model" else manifest
    target = selected.with_name("target")
    selected.rename(target)
    selected.symlink_to(target)
    with pytest.raises(ArtifactValidationError, match="regular file"):
        verify_artifact(manifest, model)


def test_directory_or_fifo_cannot_be_read_as_a_model(files):
    manifest, model = files
    model.unlink()
    model.mkdir()
    with pytest.raises(ArtifactValidationError, match="regular file"):
        verify_artifact(manifest, model)
    model.rmdir()
    if hasattr(os, "mkfifo"):
        os.mkfifo(model)
        with pytest.raises(ArtifactValidationError, match="regular file"):
            verify_artifact(manifest, model)


def test_mutation_during_stream_read_is_detected(files, monkeypatch):
    manifest, model = files
    original = os.fstat
    model_inode = model.stat().st_ino
    calls = 0

    def modified_stat(descriptor):
        nonlocal calls
        result = original(descriptor)
        if result.st_ino == model_inode:
            calls += 1
            if calls >= 3:
                values = {
                    name: getattr(result, name)
                    for name in (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                }
                values["st_mtime_ns"] += 1
                from types import SimpleNamespace

                return SimpleNamespace(**values)
        return result

    monkeypatch.setattr(os, "fstat", modified_stat)
    with pytest.raises(ArtifactValidationError, match="changed during"):
        verify_artifact(manifest, model)


def test_growing_file_is_stopped_at_declared_size_without_waiting_for_eof(files, monkeypatch):
    manifest, model = files
    original = os.fdopen
    reads = []

    class GrowingReader:
        def __init__(self, stream):
            self.stream = stream
            self.is_model = os.fstat(stream.fileno()).st_ino == model.stat().st_ino

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.stream.__exit__(*arguments)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            value = self.stream.read(size)
            if self.is_model:
                reads.append(size)
                assert len(reads) <= 3, "Verifier continued after reaching the declared model size"
                return value or b"x" * size
            return value

    monkeypatch.setattr(
        os, "fdopen", lambda *args, **kwargs: GrowingReader(original(*args, **kwargs))
    )
    with pytest.raises(ArtifactValidationError, match="changed during"):
        verify_artifact(manifest, model)
    assert reads[-1] == 1


def test_cli_works_as_a_mounted_stdlib_script_without_project_imports(files, tmp_path):
    manifest, model = files
    script = Path(artifacts.__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            "--manifest",
            str(manifest),
            "--model",
            str(model),
            "--alias",
            "qwen3-4b-ru-en-q4km-v1",
            "--context-size",
            "4096",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["artifact_verified_locally"] is True
    assert result.stderr == ""


def test_cli_returns_safe_failure_and_no_traceback_for_missing_file(files, tmp_path):
    manifest, _ = files
    marker = "private-path-marker"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(artifacts.__file__).resolve()),
            "--manifest",
            str(manifest),
            "--model",
            str(tmp_path / marker / "model.gguf"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert marker not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_does_not_echo_invalid_option_values(files):
    manifest, model = files
    marker = "private-invalid-context-marker"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(artifacts.__file__).resolve()),
            "--manifest",
            str(manifest),
            "--model",
            str(model),
            "--context-size",
            marker,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert marker not in result.stderr
    assert "Verifier arguments are invalid" in result.stderr
    assert "Traceback" not in result.stderr
