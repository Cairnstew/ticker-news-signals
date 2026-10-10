#!/usr/bin/env python3
"""Execute a built notebook end-to-end and fail on any kernel exception.

This runs the notebook against the **live** feeds (the pipeline is
partial-failure tolerant, so a dead source records on `data_quality` instead
of raising). Use it after rebuilding, or as a scheduled smoke test.

    uv run python tools/validate_notebook.py
    uv run python tools/validate_notebook.py notebooks/pipeline.ipynb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nbclient
import nbformat

DEFAULT = str(Path(__file__).resolve().parents[1] / "notebooks" / "pipeline.ipynb")


def validate(path: str | Path, *, timeout: int = 600, kernel: str = "python3") -> int:
    notebook = nbformat.read(path, as_version=4)
    client = nbclient.NotebookClient(
        notebook,
        timeout=timeout,
        kernel_name=kernel,
        resources={"metadata": {"path": str(Path(path).parent)}},
    )
    try:
        client.execute()
    except nbclient.exceptions.CellExecutionError as exc:
        # Locate the (first) failing cell for a useful message.
        failed = next(
            (
                cell
                for cell in client.nb.cells
                if cell.cell_type == "code" and cell.get("execution_count") is None
            ),
            None,
        )
        label = ""
        if failed is not None:
            first_line = failed.source.splitlines()[0] if failed.source.splitlines() else "?"
            label = f" in {first_line!r}"
        print(f"[error]{label}: {exc}", file=sys.stderr)
        return 1
    cell_count = sum(1 for cell in client.nb.cells if cell.cell_type == "code")
    print(f"{Path(path).name}: executed {cell_count} code cell(s) with no errors")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=DEFAULT)
    parser.add_argument("--timeout", type=int, default=600, help="seconds per cell")
    parser.add_argument("--kernel", default="python3")
    args = parser.parse_args()
    if not Path(args.path).is_file():
        print(f"notebook not found: {args.path}", file=sys.stderr)
        return 2
    return validate(args.path, timeout=args.timeout, kernel=args.kernel)


if __name__ == "__main__":
    raise SystemExit(main())
