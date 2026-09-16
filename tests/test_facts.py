"""Behavior tests for facts.py: each file content is measured once per content and kind, only files are
read, and any failure to measure stops the run.

Run: python3 -m unittest discover tests
"""
import json
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import facts  # noqa: E402
from git_fixtures import commit, commit_tree, run_git  # noqa: E402


class FactsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repository = self.directory / "repository"

    def test_each_file_is_read_by_its_name(self):
        # Drupal gives PHP files eight extensions, Drupal 7's .test among them.
        names = ["index.php", "node.module", "common.inc", "node.install", "olivero.theme", "standard.profile",
                 "phptemplate.engine", "node.test", "misc/drupal.js", "misc/types.ts", "node.info.yml", "composer.lock",
                 "README.txt"]
        head = commit_tree(self.repository, {name: "" for name in names})
        kinds = {entry.path: type(entry.content).__name__ for entry in facts.measure(self.repository, [head])[head]}
        self.assertEqual(kinds, {
            "index.php": "PhpContent", "node.module": "PhpContent", "common.inc": "PhpContent",
            "node.install": "PhpContent", "olivero.theme": "PhpContent", "standard.profile": "PhpContent",
            "phptemplate.engine": "PhpContent", "node.test": "PhpContent",
            "misc/drupal.js": "JavaScriptContent", "misc/types.ts": "JavaScriptContent",
            "node.info.yml": "YamlContent", "composer.lock": "ComposerLock",
            # Not read at all.
            "README.txt": "NoneType",
        })

    def test_snapshots_holding_the_same_content_share_one_measurement(self):
        # Measuring each content once is what lets all of Core's history measure in minutes.
        first = commit_tree(self.repository, {"a.module": "<?php function a() {}"})
        second = commit_tree(self.repository, {"a.module": "<?php function a() {}", "b/a.module": "<?php function a() {}",
                                               "c.module": "<?php function c() {}"})
        trees = facts.measure(self.repository, [first, second])
        # Four files over two snapshots hold two contents, each measured into one object.
        entries = [entry for snapshot in (first, second) for entry in trees[snapshot]]
        self.assertEqual((len(entries), len({id(entry.content) for entry in entries})), (4, 2))

    def test_symbolic_links_are_not_files(self):
        head = commit_tree(self.repository, {"a.module": "<?php"}, symlinks={"link.module": "a.module"})
        self.assertEqual([entry.path for entry in facts.measure(self.repository, [head])[head]], ["a.module"])

    def test_git_submodules_are_not_files(self):
        head = commit_tree(self.repository, {"a.module": "<?php"})
        # A gitlink, the tree entry git submodule add records.
        run_git(self.repository, "update-index", "--add", "--cacheinfo", f"160000,{head},library")
        run_git(self.repository, "commit", "--quiet", "--message", "Add a submodule")
        with_submodule = run_git(self.repository, "rev-parse", "HEAD").strip()
        self.assertEqual([entry.path for entry in facts.measure(self.repository, [with_submodule])[with_submodule]],
                         ["a.module"])

    def test_executable_files_are_files(self):
        # sites/default/default.settings.php was committed executable until 2012.
        commit_tree(self.repository, {"default.settings.php": "<?php"})
        (self.repository / "default.settings.php").chmod(0o755)
        executable = commit(self.repository, "Make the settings executable")
        self.assertIn("100755 blob", run_git(self.repository, "ls-tree", executable))
        self.assertEqual([entry.path for entry in facts.measure(self.repository, [executable])[executable]],
                         ["default.settings.php"])

    def test_the_same_content_read_as_two_kinds_is_measured_as_each(self):
        # An empty file is one content git holds for a YAML file and a PHP file alike.
        head = commit_tree(self.repository, {"a.yml": "", "b.module": ""})
        kinds = {entry.path: type(entry.content).__name__ for entry in facts.measure(self.repository, [head])[head]}
        self.assertEqual(kinds, {"a.yml": "YamlContent", "b.module": "PhpContent"})

    def test_a_failed_git_command_stops_the_measurement(self):
        # A repository without the commit asked for.
        commit_tree(self.repository, {"a.module": "<?php"})
        with self.assertRaisesRegex(facts.FactError, "git ls-tree"):
            facts.measure(self.repository, ["0" * 40])

    def test_anything_the_extractor_writes_to_stderr_stops_the_measurement(self):
        # PHP reports some warnings before a script's own error handler exists, and still exits 0.
        head = commit_tree(self.repository, {"a.module": "<?php"})
        warning = self.directory / "warning.php"
        warning.write_text(textwrap.dedent("""
            <?php
            stream_get_contents(STDIN);
            fwrite(STDERR, 'Deprecated: an early warning');
        """).lstrip())
        with unittest.mock.patch.object(facts, "EXTRACTOR", warning), \
                self.assertRaisesRegex(facts.FactError, "an early warning"):
            facts.measure(self.repository, [head])

    def test_a_fact_the_fact_classes_do_not_expect_stops_the_measurement(self):
        # A fact the extractor starts or stops sending would otherwise be dropped, or read as nothing.
        head = commit_tree(self.repository, {"a.js": "var a = 1;"})
        # A stand-in extractor: it reads each "<hash> blob <size>" header git cat-file writes and the content
        # after it, and answers with the one fact JavaScript files have, plus a new one.
        extended = self.directory / "extended.php"
        extended.write_text(textwrap.dedent("""
            <?php
            while (($header = fgets(STDIN)) !== false) {
              [$blob, , $size] = explode(' ', trim($header));
              stream_get_contents(STDIN, (int) $size + 1);
              echo json_encode(['blob' => $blob, 'codeLineCount' => 1, 'newFact' => 1]), "\\n";
            }
        """).lstrip())
        with unittest.mock.patch.object(facts, "EXTRACTOR", extended), \
                self.assertRaisesRegex(facts.FactError, "do not match"):
            facts.measure(self.repository, [head])

    def test_a_php_warning_stops_the_measurement(self):
        # The real extractor, whose own error handler turns a warning into a failure. A lock whose package
        # name was not a string became "Array", counted as a third-party package, while the extractor
        # exited successfully.
        head = commit_tree(self.repository, {"composer.lock": json.dumps({"packages": [{"name": ["not", "a", "string"]}]})})
        with self.assertRaisesRegex(facts.FactError, "Array to string conversion"):
            facts.measure(self.repository, [head])


if __name__ == "__main__":
    unittest.main()
