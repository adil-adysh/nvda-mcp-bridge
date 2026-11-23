#!/usr/bin/env python3
"""Extract one version's release notes from the canonical project changelog."""

import argparse
import re
from pathlib import Path


def release_notes(changelog: str, version: str) -> str:
	version = version.removeprefix("v")
	lines = changelog.splitlines()
	heading = re.compile(rf"^##\s+\[?{re.escape(version)}\]?(?:\s|$)")
	start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
	if start is None:
		raise ValueError(f"Version {version} is missing from docs/changelog.md")
	end = next(
		(index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
		len(lines),
	)
	return "\n".join(lines[start:end]).strip() + "\n"


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--version", required=True)
	parser.add_argument("--changelog", type=Path, default=Path("docs/changelog.md"))
	parser.add_argument("--output", type=Path, required=True)
	args = parser.parse_args()
	try:
		args.output.write_text(release_notes(args.changelog.read_text(encoding="utf-8"), args.version), encoding="utf-8")
	except (OSError, ValueError) as error:
		parser.error(str(error))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
