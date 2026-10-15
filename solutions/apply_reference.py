#!/usr/bin/env python3
"""Copies the gold-standard reference files into /eval/workspace (or ./workspace)."""
import os
import shutil
from pathlib import Path

solutions_dir = Path(__file__).resolve().parent / "reference_files" / "workspace"
default_workspace = Path(__file__).resolve().parent.parent / "workspace"
target_workspace = Path(os.environ.get("POLARIS_WORKSPACE", str(default_workspace))).resolve()

for src_file in solutions_dir.rglob("*.py"):
    rel = src_file.relative_to(solutions_dir)
    dst_file = target_workspace / rel
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dst_file)

print(f"Applied reference solution to {target_workspace}")
