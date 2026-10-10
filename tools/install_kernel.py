#!/usr/bin/env python3
"""Install a Jupyter kernelspec pinned to this project's interpreter.

Zed discovers notebook kernels via ``jupyter kernelspec list``. A virtualenv
ships a ``python3`` spec whose ``argv`` is a bare ``python`` resolved through
``PATH`` — so which interpreter actually runs depends on how the editor was
launched, and a notebook cell can quietly import a different
``ticker-news-signals`` (or none at all) than the one under test.

This writes a spec whose ``argv`` names the project interpreter by **absolute
path**, so the kernel is correct regardless of ``PATH``:

* ``.venv/bin/python`` when a virtualenv exists (the pip workflow), else
* the interpreter running this script (the ``nix develop`` workflow).

The spec lands in the Jupyter user data directory rather than inside
``.venv/``, which may be deleted at any time.

    python tools/install_kernel.py

Re-run it after recreating the virtualenv.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

KERNEL_NAME = "ticker-news-signals"
DISPLAY_NAME = "ticker-news-signals"

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def target_python() -> Path:
    """The interpreter the kernel should run: the venv if present, else this one."""
    if VENV_PYTHON.exists():
        return VENV_PYTHON
    return Path(sys.executable)


def user_data_dir() -> Path:
    """The Jupyter data dir whose ``kernels/`` subdir Jupyter (and Zed) scans.

    Precedence: ``JUPYTER_DATA_DIR`` env, then a project-local ``.jupyter-data``
    directory (git-ignored) when it exists, then the per-user default.
    """
    override = os.environ.get("JUPYTER_DATA_DIR")
    if override:
        return Path(override) / "kernels"
    local = ROOT / ".jupyter-data"
    if local.is_dir():
        return local / "kernels"
    return Path.home() / ".local" / "share" / "jupyter" / "kernels"


def main() -> int:
    python = target_python()
    if not python.exists():
        print(f"interpreter not found: {python}", file=sys.stderr)
        return 1

    target = user_data_dir() / KERNEL_NAME
    target.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": [str(python), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": DISPLAY_NAME,
        "language": "python",
        "metadata": {"debugger": True},
    }
    (target / "kernel.json").write_text(json.dumps(spec, indent=1) + "\n")

    print(f"installed kernelspec {KERNEL_NAME!r} -> {python}")
    print(f"  {target / 'kernel.json'}")

    # ipykernel is what actually launches; warn early rather than at cell 1.
    import importlib.util

    if importlib.util.find_spec("ipykernel") is None:
        print(
            "warning: ipykernel is not importable in this environment; "
            'install it (pip install -e ".[dev]" or `nix develop`)',
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
