#!/usr/bin/env python3
"""Bundle `whereabout` into a single portable zero-dependency executable (Python zipapp).

Output: dist/whereabout. The phone has no /usr/bin/env, so a phone build uses Termux's python:

    python3 scripts/build-whereabout.py \\
        --interpreter /data/data/com.termux/files/usr/bin/python3 \\
        --out dist/whereabout-phone
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PKG = os.path.join(ROOT, "whereabout")
DIST_DIR = os.path.join(ROOT, "dist")
OUT_PATH = os.path.join(DIST_DIR, "whereabout")


def build(interpreter: str = "/usr/bin/env python3", out_path: str = OUT_PATH) -> str:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whereabout-zipapp-") as tmp:
        shutil.copytree(
            SRC_PKG,
            os.path.join(tmp, "whereabout"),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        zipapp.create_archive(
            tmp,
            target=out_path,
            interpreter=interpreter,
            main="whereabout.cli:main",
            compressed=True,
        )
    st = os.stat(out_path)
    os.chmod(out_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a standalone whereabout executable.")
    parser.add_argument("--interpreter", default="/usr/bin/env python3")
    parser.add_argument("--out", default=OUT_PATH)
    args = parser.parse_args()
    out = build(args.interpreter, args.out)
    size_kb = os.path.getsize(out) / 1024.0
    print(f"Built standalone executable: {out} ({size_kb:.1f} KB)")
