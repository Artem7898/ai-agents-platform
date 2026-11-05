"""Check the native server key file, then run the independent artifact verifier."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

_MAX_KEY_FILE_BYTES = 16_384
_KEY_FILE = "/run/secrets/gguf_api_keys"
_MANIFEST_FILE = Path("/models/manifest.json")
_RECEIPT_DIRECTORY = Path("/verification")
_RECEIPT_FILES = (
    "model.sha256",
    "model.size",
    "manifest.sha256",
    "manifest.size",
    "model.alias",
    "context-size",
)


def validate_api_key_file(path: str) -> int:
    """Return a count only; a comments-only file must never disable native auth."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= _MAX_KEY_FILE_BYTES:
            raise ValueError
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            encoded = stream.read(_MAX_KEY_FILE_BYTES + 1)
        if not encoded or len(encoded) > _MAX_KEY_FILE_BYTES or b"\r" in encoded:
            raise ValueError
        lines = encoded.decode("ascii").split("\n")
        keys = []
        for line in lines:
            if not line or line.startswith("#"):
                continue
            if not 32 <= len(line) <= 512 or any(not 33 <= ord(char) <= 126 for char in line):
                raise ValueError
            keys.append(line)
        if not 1 <= len(keys) <= 4 or len(set(keys)) != len(keys):
            raise ValueError
        return len(keys)
    except (OSError, UnicodeError, ValueError):
        raise ValueError(
            "The GGUF API-key file must contain 1 to 4 distinct printable keys"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main() -> int:
    os.umask(0o077)
    try:
        # Invalidate a previous success before checking any new inputs.
        for name in _RECEIPT_FILES:
            (_RECEIPT_DIRECTORY / name).unlink(missing_ok=True)
        validate_api_key_file(_KEY_FILE)
        alias = os.environ["GGUF_MODEL_ALIAS"]
        context = os.environ["GGUF_CONTEXT_SIZE"]
    except (KeyError, OSError, ValueError):
        print("GGUF preflight configuration is invalid", file=sys.stderr)
        return 2
    result = subprocess.run(
        [
            sys.executable,
            "/pilot/artifacts.py",
            "--manifest",
            str(_MANIFEST_FILE),
            "--model",
            "/models/model.gguf",
            "--alias",
            alias,
            "--context-size",
            context,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print("GGUF model verification failed", file=sys.stderr)
        return result.returncode
    try:
        report = json.loads(result.stdout)
        values = {
            "model.alias": report["model_alias"],
            "context-size": str(report["context_size"]),
            "model.size": str(report["size_bytes"]),
            "manifest.size": str(report["manifest_file_size"]),
            "manifest.sha256": report["manifest_file_sha256"],
            # Publish the model hash last: a partial receipt cannot authorize startup.
            "model.sha256": report["sha256"],
        }
        for name, value in values.items():
            temporary = _RECEIPT_DIRECTORY / (name + ".tmp")
            temporary.write_text(value + "\n", encoding="ascii")
            temporary.replace(_RECEIPT_DIRECTORY / name)
    except (KeyError, OSError, TypeError, ValueError):
        print("GGUF verification receipt could not be written", file=sys.stderr)
        return 2
    print(result.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
