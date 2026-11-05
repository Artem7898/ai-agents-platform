"""Verify one declared GGUF artifact before starting an inference server.

This module deliberately uses only the Python standard library. The portable
deployment mounts it into a small verifier container without installing the
application, model libraries or provider SDKs. Source, license, quantization and
language fields are operator declarations; verification covers the local file's
size, SHA256 and basic GGUF header, not tensor correctness or model quality.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import struct
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, NoReturn
from urllib.parse import urlsplit

_MANIFEST_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_MAX_MODEL_BYTES = 1024**4
_HEADER_BYTES = 24
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
_QUANTIZATION = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,31}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_FIELDS = {
    "schema_version",
    "model_alias",
    "model_file",
    "sha256",
    "size_bytes",
    "source",
    "license",
    "quantization",
    "context_size",
    "chat_template",
    "languages",
}


class ArtifactValidationError(ValueError):
    """A bounded, public diagnostic without rejected values or file contents."""


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _https_url(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or re.search(r"[\s\x00-\x1f\x7f\\]", value)
        or "?" in value
        or "#" in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
            and not parsed.query
            and not parsed.fragment
        )
    except (ValueError, UnicodeError):
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactValidationError("Manifest contains duplicate fields")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ArtifactValidationError("Manifest must contain finite JSON values")


@contextmanager
def _regular_file(path: Path, label: str) -> Iterator[BinaryIO]:
    """Reject final symlinks/devices/FIFOs before reading, including a name race."""
    descriptor = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactValidationError(f"{label} must be a regular file without symlinks")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ArtifactValidationError(f"{label} changed while opening")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            yield stream
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactValidationError):
            raise
        raise ArtifactValidationError(f"{label} could not be read") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ArtifactValidationError("Manifest must contain exactly the documented fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ArtifactValidationError("Manifest schema version is unsupported")
    alias = value["model_alias"]
    if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
        raise ArtifactValidationError("Manifest model alias is invalid")
    if value["model_file"] != "model.gguf":
        raise ArtifactValidationError("The pilot requires one file named model.gguf")
    if not isinstance(value["sha256"], str) or _SHA256.fullmatch(value["sha256"]) is None:
        raise ArtifactValidationError("Manifest requires a lowercase SHA256 fingerprint")
    if not _integer(value["size_bytes"], _HEADER_BYTES, _MAX_MODEL_BYTES - 1):
        raise ArtifactValidationError("Manifest model size is invalid")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"url", "revision"}
        or not _https_url(source["url"])
        or not isinstance(source["revision"], str)
        or _REVISION.fullmatch(source["revision"]) is None
    ):
        raise ArtifactValidationError("Manifest requires an HTTPS source and immutable revision")
    license_info = value["license"]
    if (
        not isinstance(license_info, dict)
        or set(license_info) != {"identifier", "url"}
        or not isinstance(license_info["identifier"], str)
        or _IDENTIFIER.fullmatch(license_info["identifier"]) is None
        or not _https_url(license_info["url"])
    ):
        raise ArtifactValidationError("Manifest requires a license identifier and HTTPS reference")
    if (
        not isinstance(value["quantization"], str)
        or _QUANTIZATION.fullmatch(value["quantization"]) is None
    ):
        raise ArtifactValidationError("Manifest quantization identifier is invalid")
    if not _integer(value["context_size"], 128, 131072):
        raise ArtifactValidationError("Manifest context size is invalid")
    if value["chat_template"] != "embedded":
        raise ArtifactValidationError("The pilot uses the model's embedded chat template")
    languages = value["languages"]
    if (
        not isinstance(languages, list)
        or not 1 <= len(languages) <= 32
        or any(
            not isinstance(language, str)
            or len(language) > 63
            or _LANGUAGE.fullmatch(language) is None
            for language in languages
        )
        or len({language.casefold() for language in languages}) != len(languages)
    ):
        raise ArtifactValidationError("Manifest language tags are invalid")
    return copy.deepcopy(value)


def _manifest_record(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str, int]:
    """Bind the parsed declaration to the exact bounded bytes read once."""
    try:
        with _regular_file(Path(path), "Manifest") as stream:
            data = stream.read(_MANIFEST_BYTES + 1)
        if len(data) > _MANIFEST_BYTES:
            raise ArtifactValidationError("Manifest exceeds the size limit")
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except ArtifactValidationError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ArtifactValidationError("Manifest is not valid UTF-8 JSON") from None
    return _validate_manifest(value), hashlib.sha256(data).hexdigest(), len(data)


def load_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a bounded, strict declaration; this never downloads model weights."""
    return _manifest_record(path)[0]


def verify_artifact(
    manifest_path: str | os.PathLike[str],
    model_path: str | os.PathLike[str],
    *,
    expected_alias: str | None = None,
    context_size: int | None = None,
) -> dict[str, Any]:
    """Stream-hash one local file and return its verified public metadata.

    Header verification does not parse tensor data. A successful server load and
    authenticated protocol probes are still required before enabling a runtime.
    """
    manifest, manifest_digest, manifest_size = _manifest_record(manifest_path)
    model = Path(model_path)
    if model.name != manifest["model_file"]:
        raise ArtifactValidationError("Model path must end with the declared model.gguf filename")
    if expected_alias is not None and expected_alias != manifest["model_alias"]:
        raise ArtifactValidationError("Deployment alias does not match the manifest")
    if context_size is not None and (
        type(context_size) is not int or context_size != manifest["context_size"]
    ):
        raise ArtifactValidationError("Deployment context size does not match the manifest")
    with _regular_file(model, "Model") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != manifest["size_bytes"]:
            raise ArtifactValidationError("Model size does not match the manifest")
        header = stream.read(_HEADER_BYTES)
        if len(header) != _HEADER_BYTES or header[:4] != b"GGUF":
            raise ArtifactValidationError("Model does not have a complete GGUF header")
        version, tensors, metadata = struct.unpack("<IQQ", header[4:])
        if version not in (2, 3) or tensors == 0 or metadata == 0:
            raise ArtifactValidationError("Model GGUF header version or counts are unsupported")
        digest = hashlib.sha256(header)
        read_size = len(header)
        while chunk := stream.read(min(_CHUNK_BYTES, manifest["size_bytes"] - read_size + 1)):
            read_size += len(chunk)
            if read_size > manifest["size_bytes"]:
                raise ArtifactValidationError("Model changed during verification")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if read_size != manifest["size_bytes"] or any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise ArtifactValidationError("Model changed during verification")
        if digest.hexdigest() != manifest["sha256"]:
            raise ArtifactValidationError("Model SHA256 does not match the manifest")
    return {
        **manifest,
        "manifest_file_sha256": manifest_digest,
        "manifest_file_size": manifest_size,
        "artifact_verified_locally": True,
        "verification_scope": "sha256_size_header",
        "gguf_version": version,
        "tensor_count": tensors,
        "metadata_count": metadata,
        "weights_load_verified": False,
    }


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ArtifactValidationError("Verifier arguments are invalid")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(description="Verify one GGUF pilot artifact before startup")
    parser.add_argument("--manifest", required=True, help="Path to the manifest JSON file")
    parser.add_argument("--model", required=True, help="Path to the fixed model.gguf file")
    parser.add_argument("--alias", default=None, help="Expected deployment model alias")
    parser.add_argument("--context-size", type=int, default=None, help="Expected serving context")
    try:
        arguments = parser.parse_args(argv)
        report = verify_artifact(
            arguments.manifest,
            arguments.model,
            expected_alias=arguments.alias,
            context_size=arguments.context_size,
        )
    except ArtifactValidationError as error:
        print(f"Artifact verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
