"""Deploy changed add-on files from the repo into the installed add-on.

Copies only the files that changed, then verifies each destination by hash.
"""

import hashlib
import os
import shutil
import sys
from pathlib import Path

REPO = Path(r"D:\nvda-addons\nvda-mcp")
SRC = REPO / "addon" / "globalPlugins" / "nvdaMcp-bridge"
ADDON_ROOT = Path(os.environ["APPDATA"]) / "nvda" / "addons" / "nvdaMcpBridge" / "globalPlugins"

CHANGED = [
    r"__init__.py",
    r"server\events.py",
    r"core\__init__.py",
    r"core\base.py",
    r"core\error.py",
    r"core\executor.py",
    r"core\schema.py",
    r"tools\models.py",
    r"tools\nvda_tools.py",
    r"tools\handlers.py",
    r"validation\dataclass_validator.py",
    r"validation\output_type_registry.py",
    r"server\http_bridge.py",
    r"server\protocol_generated.py",
    r"nvda_bridge\ui.py",
    r"nvda_bridge\objects.py",
    r"nvda_bridge\focus.py",
    r"nvda_bridge\errors.py",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    target_dir = ADDON_ROOT / "nvdaMcp-bridge"
    if not target_dir.is_dir():
        print(f"target add-on tree missing: {target_dir}")
        return 2

    failures = 0
    for relative in CHANGED:
        source = SRC / relative
        target = target_dir / relative
        if not source.is_file():
            print(f"MISSING SOURCE {relative}")
            failures += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if digest(source) != digest(target):
            print(f"MISMATCH {relative}")
            failures += 1

    print(f"deployed {len(CHANGED)} files to {target_dir}, failures={failures}")

    marker = (target_dir / "server" / "http_bridge.py").read_text(encoding="utf-8")
    print("coordinator branch present:", "execution_mode is ExecutionMode.COORDINATOR" in marker)
    print("request-id echo present:", "request_id' must be a string" in marker)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
