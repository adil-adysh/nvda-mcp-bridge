#!/usr/bin/env python3
"""Package the Rust MCP companion into the add-on tree.

This is the command-line face of ``build_companion.py``; the same code runs as
an SCons action when the add-on is built, so a release cannot ship a companion
that does not match the manifest next to it.

    python scripts/package_companion.py            # cargo build --release, then stage
    python scripts/package_companion.py --no-build # stage what cargo already built
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from build_companion import main  # noqa: E402

if __name__ == "__main__":
	raise SystemExit(main())
