import unittest

from scripts.extract_release_notes import release_notes


class ReleaseNotesTests(unittest.TestCase):
	def test_extracts_only_requested_version(self):
		changelog = """# Changelog

## [0.2.0] - 2027-01-01

Upcoming.

## [0.1.0] - 2026-11-23

First release.

## 0.10.0

Internal build.
"""
		self.assertEqual(
			release_notes(changelog, "v0.1.0"),
			"## [0.1.0] - 2026-11-23\n\nFirst release.\n",
		)

	def test_accepts_unbracketed_heading(self):
		self.assertEqual(release_notes("## 0.1.0\n\nNotes\n", "0.1.0"), "## 0.1.0\n\nNotes\n")

	def test_missing_version_fails(self):
		with self.assertRaisesRegex(ValueError, "missing"):
			release_notes("## 0.2.0\n", "0.1.0")


if __name__ == "__main__":
	unittest.main()
