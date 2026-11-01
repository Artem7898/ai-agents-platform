"""Install the wheel and locked runtime dependencies outside the checkout."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    wheels = sorted((project / "dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise SystemExit("Build a wheel first: uv build --wheel")
    wheel = wheels[-1]
    with tempfile.TemporaryDirectory(prefix="agents-wheel-") as directory:
        root = Path(directory)
        requirements = subprocess.check_output(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
            ],
            cwd=project,
        )
        locked = root / "requirements.txt"
        locked.write_bytes(requirements)
        environment = root / "venv"
        subprocess.run(["uv", "venv", str(environment), "--python", sys.executable], check=True)
        python = environment / "bin" / "python"
        subprocess.run(["uv", "pip", "sync", "--python", str(python), str(locked)], check=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
            check=True,
        )
        # -I prevents PYTHONPATH or the checkout's cwd from hiding a broken wheel.
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', "
                "'config.settings.test'); import django; django.setup(); "
                "import config.asgi; import src.runs.executor; "
                "print('wheel import smoke passed')",
            ],
            cwd=root,
            check=True,
        )


if __name__ == "__main__":
    main()
