"""Slice an exported project with the real Snapmaker Orca CLI.

usage: .venv/bin/python tests/orca_slice.py project.3mf [outdir]
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from orca_cli import slice_project  # noqa: E402

src = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="kaicho_slice_")).resolve()
try:
    print("OK:", out, slice_project(src, out))
except RuntimeError as e:
    print("ERROR:", e, "| log:", out / "orca.log")
    sys.exit(1)
