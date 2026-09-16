"""Behavior tests for explain.py: the items behind a number, and what a changed definition moves.

Run: python3 -m unittest discover tests
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import definitions  # noqa: E402
from git_fixtures import commit_tree  # noqa: E402

SCRIPTS = Path(definitions.__file__).parent


class ExplainTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repository = self.directory / "repository"
        self.commit = commit_tree(self.repository, {"foo.module": "<?php\nfunction foo_load() {\n}\nfunction _foo_helper() {\n}\n"})

    def explain(self, *arguments: str) -> str:
        return subprocess.run([sys.executable, str(SCRIPTS / "explain.py"), *arguments, "--repository", str(self.repository)],
                              capture_output=True, text=True, check=True).stdout

    def test_lists_the_items_behind_a_definition(self):
        # _foo_helper() is internal, not a global function.
        self.assertEqual(self.explain("global_functions", self.commit[:10]), "foo_load\n1 items\n")

    def test_lists_what_a_changed_definition_adds_and_drops(self):
        # The changed copy calls functions starting with foo internal instead of those starting with _.
        rule = 'declaration.name.startswith("_")'
        source = (SCRIPTS / "definitions.py").read_text()
        self.assertEqual(source.count(rule), 1)
        changed = self.directory / "changed.py"
        changed.write_text(source.replace(rule, 'declaration.name.startswith("foo")'))
        self.assertEqual(self.explain("global_functions", self.commit[:10], "--against", str(changed)),
                         f"+ _foo_helper\n- foo_load\n1 items added and 1 dropped by {changed}\n")

    def test_an_unchanged_copy_moves_nothing(self):
        # The copy defines a File class of its own, so its files must still read as the same files.
        unchanged = self.directory / "unchanged.py"
        unchanged.write_text((SCRIPTS / "definitions.py").read_text())
        self.assertEqual(self.explain("production_php", self.commit[:10], "--against", str(unchanged)),
                         f"0 items added and 0 dropped by {unchanged}\n")


if __name__ == "__main__":
    unittest.main()
