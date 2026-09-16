"""Behavior tests for the codebase definitions (definitions.py), one class per section of that file.

Each test commits a small tree to a real git repository, measures it with facts.py, and asserts on the
snapshot fields data.json would carry, pinning one rule with forms Drupal Core's history holds. Where a
rule replaced one that counted the wrong thing, a comment says what went wrong. Most PHP files are one
line, so a line count is a count of files.

Run: python3 -m unittest discover tests
"""
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import definitions  # noqa: E402
import facts  # noqa: E402
from git_fixtures import commit_tree  # noqa: E402


def measure_snapshot(files: dict[str, str]) -> definitions.Snapshot:
    """A snapshot of a tree holding these files, measured from a real git repository."""
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory) / "repository"
        commit = commit_tree(repository, {path: textwrap.dedent(content).lstrip("\n") for path, content in files.items()})
        return definitions.Snapshot(facts.measure(repository, [commit])[commit])


def snapshot_fields(files: dict[str, str]) -> dict:
    """The snapshot fields of a tree holding these files, plus unparsedFiles and codeLines, the lines
    source code age dates."""
    snapshot = measure_snapshot(files)
    fields = snapshot.fields()
    fields["unparsedFiles"] = len(snapshot.unparsed_production_php)
    fields["codeLines"] = snapshot.production_code_lines()
    return fields


def function_names(snapshot: dict) -> list[str]:
    return [function["name"] for function in snapshot["hotspots"]["functions"]]


def function_scores(snapshot: dict, score: str) -> list[tuple[str, int]]:
    """Every hotspot function's name and one of its scores, sorted by name. A list rather than a
    mapping, because two anonymous classes in one file can declare methods of the same name."""
    return sorted((function["name"], function[score]) for function in snapshot["hotspots"]["functions"])


class FilesTest(unittest.TestCase):
    def test_third_party_php_is_not_drupal_code(self):
        snapshot = snapshot_fields({
            "modules/node/node.module": "<?php function node_load() {}",
            # Third-party code sits in the vendor/ and assets/ directories at the root.
            "vendor/symfony/kernel.php": "<?php function vendor_function() {}",
            "assets/scaffold/files/default.settings.php": "<?php function asset_function() {}",
            # Libraries Core once kept in its own tree, left out by name: the Doctrine annotation parser,
            # XML-RPC for PHP, XTemplate and PEAR's Archive_Tar.
            "lib/Drupal/Component/Annotation/Doctrine/DocParser.php": "<?php function doctrine_function() {}",
            "includes/xmlrpc.inc": "<?php function xmlrpc_function() {}",
            "includes/xmlrpcs.inc": "<?php function xmlrpc_server_function() {}",
            "themes/engines/xtemplate/xtemplate.inc": "<?php function xtemplate_function() {}",
            "modules/system/system.tar.inc": "<?php function tar_function() {}",
            "lib/Drupal/Component/Archiver/ArchiveTar.php": "<?php function archive_tar_function() {}",
        })
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["node_load"])

    def test_code_under_core_is_the_root_once_core_moved_there(self):
        # Drupal 8 moved Core's code into core/. From then on only core/ is read, and paths are read from
        # there, so a file keeps its path across the move.
        snapshot = snapshot_fields({
            "core/modules/node/node.module": "<?php function node_load() {}",
            "index.php": "<?php function outside_core() {}",
        })
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["node_load"])
        self.assertEqual(snapshot["hotspots"]["functions"][0]["file"], "modules/node/node.module")

    def test_a_php_script_counts_whatever_its_extension(self):
        # Core's command-line scripts carry .sh or no extension at all and went uncounted, run-tests.sh
        # with them: it opens with <?php, password-hash.sh with a php shebang, and commit-code-check.sh
        # is a bash script.
        snapshot = snapshot_fields({
            "scripts/run-tests.sh": """
                <?php
                function simpletest_script_run() {}
            """,
            "scripts/password-hash.sh": """
                #!/usr/bin/env php
                <?php
                function print_help() {}
            """,
            "scripts/dev/commit-code-check.sh": """
                #!/bin/bash
                echo "Checking"
            """,
        })
        # Two code lines each: a shebang is a line for the operating system rather than code. The bash
        # script is not PHP and counts nothing.
        self.assertEqual((snapshot["production"]["lines"], snapshot["surfaceArea"]["globalFunctions"]),
                         (4, ["print_help", "simpletest_script_run"]))

    def test_identical_content_counts_once(self):
        # The CVS-to-git migration rebuilt renamed modules' histories at their later paths and names,
        # so from 2001 to 2006 git holds copies no release had: node.module beside node/node.module,
        # and import.module beside aggregator.module, the name it took in 2004.
        node = "<?php function node_load() {}"
        aggregator = "<?php function aggregator_page() {}"
        snapshot = snapshot_fields({
            "modules/node.module": node,
            "modules/node/node.module": node,
            "modules/aggregator.module": aggregator,
            "modules/import.module": aggregator,
        })
        self.assertEqual(snapshot["production"]["lines"], 2)

    def test_templates_and_hook_documentation_are_not_code(self):
        # .tpl.php templates are markup, like the Twig templates that replaced them, and .api.php files
        # document hooks.
        snapshot = snapshot_fields({
            "modules/node/node.module": "<?php function node_load() {}",
            "modules/node/node.tpl.php": "<div><?php print $content; ?></div>",
            "modules/node/node.api.php": "<?php function hook_node_load() {}",
        })
        self.assertEqual(snapshot["production"]["lines"], 1)


class CodeClassificationTest(unittest.TestCase):
    def test_test_code_is_in_test_directories_and_test_classes(self):
        # Test code is in a directory whose name starts or ends with test or Test, in a file named
        # *Test.php or *TestBase.php, or in a .test file. It was a list of directories, so the simpletest
        # framework, the testing profiles and Drupal 7's .test files counted as production.
        snapshot = snapshot_fields({
            "modules/node/node.module": "<?php function node_load() {}",
            # Six test files.
            "modules/node/node.test": "<?php class NodeTestCase {}",
            "modules/simpletest/drupal_web_test_case.php": "<?php class DrupalWebTestCase {}",
            "profiles/testing/testing.profile": "<?php function testing_install() {}",
            "lib/Drupal/Core/Config/Testing/ConfigSchemaChecker.php": "<?php class ConfigSchemaChecker {}",
            "lib/Drupal/KernelTestBase.php": "<?php class KernelTestBase {}",
            "tests/bootstrap.php": "<?php function drupal_phpunit_find_extension_directories() {}",
        })
        self.assertEqual((snapshot["production"]["lines"], snapshot["test"]["lines"]), (1, 6))

    def test_generated_files_count_as_nothing(self):
        snapshot = snapshot_fields({
            # Generated, as their opening comments say: neither production nor test code, though the dump
            # sits in a tests directory.
            "modules/simpletest/tests/upgrade/drupal-6.database.php": """
                <?php
                /**
                 * This file was generated by the dump-database-d6.sh tool.
                 */
                db_insert('node')->execute();
            """,
            "lib/Drupal/Core/ProxyClass/Lock/LockBackend.php": """
                <?php
                /**
                 * THIS IS A GENERATED FILE. DO NOT EDIT.
                 */
                class LockBackend {}
            """,
            # Production: the generator holds the marker only in the string it writes into its output.
            "lib/Drupal/Core/Command/GenerateProxyClassCommand.php":
                "<?php class GenerateProxyClassCommand { const HEADER = '/** This file was generated via a script. */'; }",
        })
        self.assertEqual((snapshot["production"]["lines"], snapshot["test"]["lines"]), (1, 0))

    def test_a_test_file_of_literal_values_is_data_too(self):
        # Production leaves out files that declare and call nothing, and tests counted them: core's
        # test themes and test modules include stubs that hold an opening tag and nothing else.
        snapshot = snapshot_fields({
            "modules/node/tests/node_test.module": "<?php function node_test_help() {}",
            "modules/system/tests/themes/test_theme_phptemplate/test_theme_phptemplate.theme": "<?php",
        })
        self.assertEqual(snapshot["test"]["lines"], 1)

    def test_a_file_of_literal_values_is_data(self):
        snapshot = snapshot_fields({
            # A transliteration table declares and calls nothing.
            "lib/Drupal/Component/Transliteration/data/x00.php": "<?php $base = [0x00 => 'a'];",
            # Code: a function makes a file code, and so does a call, even t() around a label.
            "includes/iso.inc": "<?php function country_list() { return ['BE' => 'Belgium']; }",
            "themes/garland/color/color.inc": "<?php $info = ['schemes' => ['default' => t('Blue Lagoon')]];",
        })
        self.assertEqual(snapshot["production"]["lines"], 2)

    def test_test_fixtures_count_as_neither_production_nor_test(self):
        snapshot = snapshot_fields({
            # A database dump with no generated-file header: its fixtures directory leaves it out.
            "modules/migrate_drupal/tests/fixtures/drupal7.php": "<?php $connection->insert('node')->execute();",
            "modules/migrate_drupal/tests/src/Kernel/MigrateDrupalTestBase.php": "<?php class MigrateDrupalTestBase {}",
        })
        self.assertEqual((snapshot["production"]["lines"], snapshot["test"]["lines"]), (0, 1))

    def test_pre_php_8_string_offsets_still_parse(self):
        # PHP 8 removed $string{0} offsets, and files using them failed to parse, losing every function
        # they declared (common.inc in 2004).
        snapshot = snapshot_fields({"includes/common.inc": "<?php function check_url($uri) { return $uri{0} == '/'; }"})
        self.assertEqual(function_names(snapshot), ["check_url"])

    def test_short_open_tags_are_php(self):
        # Drupal opened files with "<?" until 2007.
        self.assertEqual(function_names(snapshot_fields({"modules/node.module": "<? function node_load() {}"})), ["node_load"])

    def test_a_file_that_does_not_parse_counts_its_lines_and_declares_nothing(self):
        snapshot = snapshot_fields({
            "includes/broken.inc": "<?php function broken( {",
            "includes/common.inc": "<?php function check_plain() {}",
        })
        self.assertEqual((snapshot["unparsedFiles"], snapshot["production"]["lines"], function_names(snapshot)),
                         (1, 2, ["check_plain"]))


class LinesOfCodeTest(unittest.TestCase):
    def test_functions_are_procedural_even_in_a_file_that_declares_a_class(self):
        # The object-oriented share was decided per file, so pre-2012 node.module and database.inc
        # counted all their functions as object-oriented because each declared one class: 18,431
        # object-oriented lines in 2011 against about 8,120 lines of class bodies.
        production = snapshot_fields({
            "includes/database.inc": """
                <?php
                function db_query() {
                  return 1;
                }
                class DatabaseConnection {
                  public function query() { return 2; }
                }
            """,
        })["production"]
        # Object-oriented: the class's 3 lines, and <?php, a line outside both, which counts as
        # object-oriented because the file declares a class. Procedural: the function's 3 lines.
        self.assertEqual((production["lines"], production["objectOrientedLines"]), (7, 4))

    def test_a_file_without_classes_stays_procedural(self):
        production = snapshot_fields({
            "modules/node/node.module": """
                <?php
                function node_load() {
                  return 1;
                }
            """,
        })["production"]
        # <?php and the function's 3 lines: a line outside functions is procedural when the file declares no class.
        self.assertEqual((production["lines"], production["objectOrientedLines"]), (4, 0))

    def test_classes_interfaces_traits_and_enums_are_object_oriented(self):
        # Three lines each, all object-oriented: <?php and the namespace count with the class-like the file declares.
        production = snapshot_fields({
            "src/Square.php": """
                <?php
                namespace Shapes;
                class Square {}
            """,
            "src/Shape.php": """
                <?php
                namespace Shapes;
                interface Shape {}
            """,
            "src/Named.php": """
                <?php
                namespace Shapes;
                trait Named {}
            """,
            "src/Suit.php": """
                <?php
                namespace Shapes;
                enum Suit {}
            """,
        })["production"]
        self.assertEqual((production["lines"], production["objectOrientedLines"]), (12, 12))

    def test_an_anonymous_class_does_not_make_a_file_object_oriented(self):
        # An anonymous class is an inline throwaway, and counting it as a declaration made the file's
        # own lines object-oriented: views_ui/admin.inc builds one and declares no class of its own.
        production = snapshot_fields({
            "modules/views_ui/admin.inc": """
                <?php
                use Drupal\\Core\\Form\\FormBase;
                function views_ui_standard_display_dropdown($form) {
                  return $form;
                }
                $handler = new class extends FormBase {
                  public function build() { return []; }
                };
            """,
        })["production"]
        # The anonymous class's three lines are object-oriented; the file's other five, the opening tag,
        # the use statement and the function, are procedural.
        self.assertEqual((production["lines"], production["objectOrientedLines"]), (8, 3))

    def test_a_line_inside_a_string_is_code(self):
        # Lines were read by their first characters, so the lines of a string that start with * or //
        # counted as comments. Core's proxy class builder holds the class it generates as a heredoc
        # opening with a docblock, and every line of that docblock was read as a comment.
        snapshot = snapshot_fields({
            "lib/Drupal/Component/ProxyBuilder/ProxyBuilder.php": """
                <?php
                class ProxyBuilder {
                  public function build($class) {
                    return <<<EOS
                /**
                 * A generated proxy.
                 */
                class {$class}Proxy {}
                EOS;
                  }
                }
            """,
        })
        # Every line of the file but its blank ones: the three lines of the docblock inside the string
        # are code, because the string is.
        self.assertEqual(snapshot["production"]["lines"], 11)

    def test_javascript_and_typescript_count_their_code_lines(self):
        snapshot = snapshot_fields({
            "misc/drupal.js": """
                var a = 1;
                // A comment.
                var b = 2;
            """,
            "misc/types.ts": "let value: number = 1;",
        })
        # drupal.js 2 lines, types.ts 1.
        self.assertEqual(snapshot["js"]["lines"], 3)

    def test_a_compiled_twin_counts_as_its_es6_source(self):
        # From 2017 to 2022 Core kept .es6.js sources compiled into a .js twin, and counting the twin
        # put a 17% step in the series when the build was dropped.
        snapshot = snapshot_fields({
            "misc/ajax.es6.js": "const ajax = 1;",
            "misc/ajax.js": """
                var ajax = 1;
                var compiled = true;
            """,
            "misc/drupal.js": "var drupal = 1;",
        })
        # ajax.es6.js and drupal.js, one line each.
        self.assertEqual(snapshot["js"]["lines"], 2)

    def test_bundled_libraries_and_minified_javascript_are_not_drupal_code(self):
        # Until June 2013 Core kept third-party JavaScript beside its own. Counting VIE, Create.js and
        # picturefill drew a JavaScript spike at the end of 2012 that was not Drupal's code.
        line = "var value = 1;"
        snapshot = snapshot_fields({
            # Drupal's own, one line each, even where the name starts with jquery.
            "misc/drupal.js": line,
            "misc/jquery.tabbable.shim.js": line,
            # Minified files.
            "misc/drupal.min.js": line,
            # The places Core kept third-party JavaScript before core/assets/vendor.
            "misc/jquery.js": line,
            "misc/jquery.form.js": line,
            "misc/html5.js": line,
            "misc/ui/ui.core.js": line,
            "misc/ui/external/globalize.js": line,
            "misc/farbtastic/farbtastic.js": line,
            "misc/backbone/backbone.js": line,
            "misc/underscore/underscore.js": line,
            "misc/modernizr/modernizr.js": line,
            "misc/vie/vie-core.js": line,
            "misc/create/create-editonly.js": line,
            "modules/picture/picturefill/picturefill.js": line,
            "modules/tour/js/jquery.joyride-2.0.3.js": line,
            # Third-party code in assets/ and vendor/.
            "assets/vendor/once/once.js": line,
            "assets/scaffold/files/scaffold.js": line,
            "vendor/package/package.js": line,
        })
        self.assertEqual(snapshot["js"]["lines"], 2)

    def test_only_the_assets_directory_at_the_root_is_third_party(self):
        # Third-party front-end files sit in Core's own assets/, but a directory of that name anywhere
        # left out Core's code: the JavaScript helpers it keeps in core/scripts/js/assets since 2022.
        line = "var value = 1;"
        snapshot = snapshot_fields({
            "assets/vendor/once/once.js": line,
            "scripts/js/assets/process/map.js": line,
        })
        self.assertEqual(snapshot["js"]["lines"], 1)

    def test_javascript_in_test_code_is_not_production(self):
        line = "var value = 1;"
        snapshot = snapshot_fields({"misc/drupal.js": line, "modules/node/tests/node.js": line,
                            "modules/simpletest/simpletest.js": line})
        self.assertEqual(snapshot["js"]["lines"], 1)

    def test_source_code_age_dates_exactly_the_counted_lines(self):
        snapshot = snapshot_fields({
            "core/includes/bootstrap.inc": """
                <?php
                // Bootstrap.
                function drupal_bootstrap() {

                  return 1;
                }
            """,
            "core/modules/node/node.test": "<?php class NodeTestCase {}",
        })
        # Comments, blank lines and test code are not dated. Paths keep core/, since git blame reads the
        # repository.
        self.assertEqual(snapshot["codeLines"], {"core/includes/bootstrap.inc": [1, 3, 5, 6]})


class DependenciesTest(unittest.TestCase):
    LOCK = json.dumps({"packages": [{"name": "symfony/http-kernel"}, {"name": "drupal/core"}],
                       "packages-dev": [{"name": "phpunit/phpunit"}, {"name": "drupal/coder"}]})

    def test_drupal_packages_are_not_third_party_dependencies(self):
        self.assertEqual(snapshot_fields({"composer.lock": self.LOCK})["dependencies"], {"production": 1, "development": 1})

    def test_a_snapshot_without_a_lock_has_no_dependency_count(self):
        self.assertIsNone(snapshot_fields({"index.php": "<?php"})["dependencies"])

    def test_lock_under_core_is_found(self):
        # The lock lived at core/composer.lock during the Drupal 8 cycle before moving to the
        # repository root. Reading only the root turned those snapshots into nulls, which the
        # chart drops, hiding the gap.
        self.assertEqual(snapshot_fields({"core/composer.lock": self.LOCK})["dependencies"], {"production": 1, "development": 1})

    def test_null_dev_packages_report_not_measured_rather_than_zero(self):
        # A 2013-era lock carries "packages-dev": null. Those locks predate the require-dev split,
        # and 0 would draw a flat "no dev dependencies" line the lock never claimed.
        lock = json.dumps({"packages": [{"name": "symfony/yaml"}], "packages-dev": None})
        self.assertEqual(snapshot_fields({"composer.lock": lock})["dependencies"], {"production": 1, "development": None})

    def test_early_locks_name_each_package_under_package(self):
        # 2012-2013 Core locks use "package" where later ones use "name".
        lock = json.dumps({"packages": [{"package": "drupal/core"}, {"package": "symfony/yaml"}], "packages-dev": []})
        self.assertEqual(snapshot_fields({"composer.lock": lock})["dependencies"], {"production": 1, "development": 0})


class ComplexityTest(unittest.TestCase):
    def test_bodiless_signatures_do_not_dilute_the_aggregate(self):
        # About 3,000 interface and abstract signatures scored 0 and pulled Core's 95th
        # percentile from 13 to 11.
        snapshot = snapshot_fields({
            "Example.php": """
                <?php
                interface Shape { public function area(): float; public function name(): string; }
                abstract class Base { abstract public function build(): array; }
                class Square extends Base {
                  public function build(): array {
                    if ($this->ready) { if ($this->valid) { return []; } }
                    return [1];
                  }
                }
            """,
        })
        # Square::build, the one function with a body, scores 3: 1 for the if and 2 for the if nested in it.
        self.assertEqual(snapshot["production"]["cognitive"], {"average": 3, "percentile95": 3})

    def test_the_95th_percentile_is_the_value_at_ceil_95_percent(self):
        def percentile95(count: int) -> int:
            # Function number n holds n ifs in a row and scores n, so the scores run from 0 to count - 1.
            functions = ["function score_%d($a) { %s}" % (number, "if ($a) {} " * number) for number in range(count)]
            return snapshot_fields({"scores.module": "<?php\n" + "\n".join(functions)})["production"]["cognitive"]["percentile95"]
        # The ceil(0.95 n)-th smallest score is ceil(0.95 n) - 1. Of 12, 11.4 goes up to the 12th rather than
        # rounding down; of 20, exactly 19 stays the 19th; of 101, 95.95 goes up to the 96th.
        self.assertEqual([percentile95(12), percentile95(20), percentile95(101)], [11, 18, 95])

    def test_recursion_scores_once_for_a_declaration_that_calls_itself(self):
        # Recursion scored once per call, and a call matched the declaration by name alone: a method
        # calling itself twice scored 2, and FileSystem::chmod() calling PHP's chmod() scored 1.
        snapshot = snapshot_fields({
            "includes/form.inc": "<?php function form_select_options($element) { return form_select_options($element); }",
            "src/FileSystem.php": "<?php class FileSystem { public function chmod($uri) { return chmod($uri); } }",
            "src/OptGroup.php": "<?php class OptGroup { public static function doFlatten($options) { return static::doFlatten($options); } }",
            "src/FilterPluginBase.php": """
                <?php
                class FilterPluginBase {
                  public function prepareOptions($options) {
                    foreach ($options as $option) { $this->prepareOptions($option); }
                    return $this->prepareOptions([]);
                  }
                }
            """,
        })
        # A declaration reaches itself as a function by its name, and as a method through $this->, self::
        # or static::. prepareOptions scores 1 for the foreach and 1 for the recursion it reaches twice.
        self.assertEqual(function_scores(snapshot, "cognitive"),
                         [("FileSystem::chmod", 0), ("FilterPluginBase::prepareOptions", 2),
                          ("OptGroup::doFlatten", 1), ("form_select_options", 1)])

    def test_two_word_else_if_is_an_elseif_and_not_a_nested_if(self):
        # PHP parses "else if" as an else holding an if, the tree it also gives else { if ... }, and it
        # scored as the nested form. Core's theme settings spelled it in two words until 2016.
        snapshot = snapshot_fields({
            "includes/theme.inc": """
                <?php
                function settings_else_if($key) {
                  if ($key == 'logo') { return 1; }
                  else if ($key == 'favicon') { return 2; }
                  else { return 3; }
                }
                function settings_elseif($key) {
                  if ($key == 'logo') { return 1; }
                  elseif ($key == 'favicon') { return 2; }
                  else { return 3; }
                }
                function settings_else_braced($key) {
                  if ($key == 'logo') { return 1; }
                  else { if ($key == 'favicon') { return 2; } }
                }
            """,
        })
        # Both spellings of the chain score 3, one each for the if, the elseif and the else. The braced
        # else scores 1, and the if inside it 1 for itself and 1 for sitting a level deeper.
        self.assertEqual(function_scores(snapshot, "cognitive"),
                         [("settings_else_braced", 4), ("settings_else_if", 3), ("settings_elseif", 3)])

    def test_a_jump_out_of_more_than_one_loop_scores(self):
        # PHP writes the paper's multi-level jumps as break and continue with a level, which scored
        # nothing. Core has written them since 2004 and writes 32 of them today.
        snapshot = snapshot_fields({
            "modules/system/system.module": """
                <?php
                function system_check($rows) {
                  foreach ($rows as $row) {
                    foreach ($row as $cell) { continue 2; }
                  }
                  foreach ($rows as $row) { break 1; }
                }
            """,
        })
        # 1 for the outer loop, 2 for the loop nested in it, 1 for the continue leaving both, and 1 for the
        # last loop. Nothing for break 1, which leaves one loop, as a break with no level does.
        self.assertEqual(function_scores(snapshot, "cognitive"), [("system_check", 5)])

    def test_a_fallback_arm_is_not_a_cyclomatic_decision_and_a_coalescing_assignment_is(self):
        # A switch's default and a match's default arm are where a value falls through rather than a
        # branch taken on a value, and each scored 1; ??= branches on null as ?? does and scored nothing.
        snapshot = snapshot_fields({
            "src/Formatter.php": """
                <?php
                class Formatter {
                  public function label($type, $options) {
                    $options['label'] ??= 'none';
                    switch ($type) {
                      case 'node': return 'Node';
                      default: return $options['label'];
                    }
                  }
                  public function icon($type) {
                    return match ($type) {
                      'node' => 'file',
                      default => 'dot',
                    };
                  }
                }
            """,
        })
        # Cyclomatic complexity is 1 plus each decision: for label the case and the ??=, for icon the one
        # arm that matches a value.
        self.assertEqual(function_scores(snapshot, "cyclomatic"), [("Formatter::icon", 2), ("Formatter::label", 3)])

    def test_a_method_keeps_its_scores_after_a_nested_class(self):
        # The old analyzer tracked "the current function" in one variable that leaving a nested
        # declaration cleared, and lost everything after it.
        snapshot = snapshot_fields({
            "src/Builder.php": """
                <?php
                class Builder {
                  public function build($form) {
                    $handler = new class { public function run() { return 1; } };
                    if ($form) { $form['a']['b']['c'] = \\Drupal::service('x'); }
                    return $handler;
                  }
                }
            """,
        })
        build = next(function for function in snapshot["hotspots"]["functions"] if function["name"] == "Builder::build")
        # Cognitive complexity 1 for the if; cyclomatic complexity 1 plus 1 for the if; antipatterns 1 for the
        # three-level access's level beyond two, and 1 for the service locator.
        self.assertEqual(build, {"name": "Builder::build", "file": "src/Builder.php", "cognitive": 1, "cyclomatic": 2,
                                 "antipatterns": 2})

    def test_same_named_methods_of_two_anonymous_classes_are_two_functions(self):
        # The old analyzer keyed functions by file and name, so the second overwrote the first.
        snapshot = snapshot_fields({
            "src/Handlers.php": """
                <?php
                function handlers() {
                  $first = new class { public function run($a) { if ($a) { return 1; } } };
                  $second = new class { public function run() { return 2; } };
                  return [$first, $second];
                }
            """,
        })
        self.assertEqual(function_scores(snapshot, "cognitive"),
                         [("anonymous::run", 0), ("anonymous::run", 1), ("handlers", 0)])


class ClassCohesionTest(unittest.TestCase):
    # LCOM4 counts a class's jobs: the groups of methods that share no property and call nothing in
    # another group. data.json holds the average over classes, rounded to two decimals.

    def test_create_and_the_constructor_it_calls_are_one_job(self):
        # Every dependency-injected class scored one extra job when create() and the
        # constructor it feeds through new static() were not linked.
        snapshot = snapshot_fields({
            "src/Controller.php": """
                <?php
                class Controller {
                  public static function create($container) { return new static($container->get('a')); }
                  public function __construct($service) { $this->service = $service; }
                  public function build() { return $this->service->build(); }
                }
            """,
        })
        self.assertEqual(snapshot["production"]["lcom4"], 1)

    def test_abstract_methods_are_not_jobs(self):
        # Each abstract signature was a job of its own and scored 2011's abstract base classes as
        # doing several (3.26 instead of 2.97). A class of signatures alone has no LCOM4 and is left
        # out of the average.
        snapshot = snapshot_fields({
            "src/Base.php": """
                <?php
                abstract class Base {
                  abstract public function label();
                  abstract public function id();
                  public function name() { return $this->name; }
                }
                abstract class OnlySignatures {
                  abstract public function build();
                }
            """,
        })
        self.assertEqual(snapshot["production"]["lcom4"], 1)

    def test_named_classes_traits_and_enums_count_and_anonymous_classes_do_not(self):
        snapshot = snapshot_fields({
            "src/Kinds.php": """
                <?php
                trait Greets { public function hi() { return $this->a; } public function bye() { return $this->b; } }
                class Single { public function get() { return $this->x; } }
                enum Suit { case Hearts; public function label() { return 'hearts'; } }
                $handler = new class { public function a() {} public function b() {} };
            """,
        })
        # Greets 2, Single 1, Suit 1: 1.33. Anonymous classes are inline throwaways, so the average leaves
        # them out, though their methods still rank as hotspots; this one's 2 would make it 1.5.
        self.assertEqual(snapshot["production"]["lcom4"], 1.33)


class AntipatternsTest(unittest.TestCase):
    def test_magic_keys_score_each_render_array_property(self):
        snapshot = snapshot_fields({
            "modules/foo/foo.module": """
                <?php
                function foo_build($form) {
                  $form['#weight_extra'] = 1;
                  return ['#type' => 'x', '#ffe23d,#a9290a' => 'Garland'];
                }
            """,
        })
        # A magic key is # and a name: a lowercase letter, then letters, digits or underscores. Each use
        # scores, the access and #type here; Garland's color scheme, keyed '#ffe23d,#a9290a', is data.
        self.assertEqual(snapshot["production"]["antipatterns"], {"magicKeys": 2, "deepArrays": 0, "serviceLocators": 0})

    def test_deep_arrays_score_each_level_beyond_two(self):
        snapshot = snapshot_fields({
            "modules/foo/foo.module": """
                <?php
                function foo_build($form) {
                  $shallow = $form['a']['b'];
                  $deep = $form['a']['b']['c']['d'];
                  $again = $form['a']['b']['c']['d'];
                  $nested = [[[1]]];
                  return $deep;
                }
            """,
        })
        # Each four-level access scores 2, the three-level literal 1.
        self.assertEqual(snapshot["production"]["antipatterns"], {"magicKeys": 0, "deepArrays": 5, "serviceLocators": 0})

    def test_service_locators_score_each_call(self):
        snapshot = snapshot_fields({
            "modules/foo/foo.module": """
                <?php
                function foo_build($object) {
                  $service = $object->container->get('x');
                  $maybe = $object->container?->get('y');
                  \\Drupal::service('y');
                  \\drupal::config('z');
                  $title = $object->get('title');
                  return $service;
                }
            """,
        })
        # \Drupal:: calls and ->container->get() fetch a dependency instead of receiving it; PHP class names
        # are case-insensitive. An entity's ->get() reads a field.
        self.assertEqual(snapshot["production"]["antipatterns"], {"magicKeys": 0, "deepArrays": 0, "serviceLocators": 4})

    def test_antipatterns_score_the_function_they_sit_in(self):
        # A method of an anonymous class is a function of its own, not part of the method creating it.
        snapshot = snapshot_fields({
            "src/Builder.php": """
                <?php
                class Builder {
                  public function build() {
                    return new class { public function run() { return \\Drupal::service('x'); } };
                  }
                }
            """,
        })
        self.assertEqual(function_scores(snapshot, "antipatterns"),
                         [("Builder::build", 0), ("anonymous::run", 1)])


class HotspotsTest(unittest.TestCase):
    def test_ties_rank_in_file_and_declaration_order(self):
        snapshot = snapshot_fields({
            "b.module": "<?php function b_equal() {}",
            "a.module": """
                <?php
                function a_equal() {}
                class Holder {
                  public function held() {}
                }
            """,
        })
        # Every function scores 0, so a.module's functions come first, in the order they are declared.
        self.assertEqual(function_names(snapshot), ["a_equal", "Holder::held", "b_equal"])

    def test_hotspots_rank_the_most_complex_first(self):
        snapshot = snapshot_fields({
            "a.module": "<?php class Easy { public function run() {} }",
            "b.module": "<?php class Hard { public function run($a) { if ($a) {} } }",
        })
        # By cognitive complexity: Hard::run scores 1, Easy::run 0.
        self.assertEqual((function_names(snapshot), [entry["name"] for entry in snapshot["hotspots"]["classes"]]),
                         (["Hard::run", "Easy::run"], ["Hard", "Easy"]))

    def test_hotspots_list_at_most_fifty_functions_and_classes(self):
        code = "<?php\n" + "\n".join("class Listed%d { public function run() {} }" % number for number in range(51))
        hotspots = snapshot_fields({"src/Listed.php": code})["hotspots"]
        self.assertEqual((len(hotspots["functions"]), len(hotspots["classes"])), (50, 50))

    def test_each_class_declaration_is_its_own_entry(self):
        # Two classes named Holder in two namespaces, and two anonymous classes in one file, are four classes.
        snapshot = snapshot_fields({
            "src/A/Holder.php": "<?php namespace A; class Holder { public function run() {} }",
            "src/B/Holder.php": "<?php namespace B; class Holder { public function run() {} }",
            "src/Two.php": "<?php $first = new class { public function a() {} }; $second = new class { public function b() {} };",
        })
        self.assertEqual(sorted((entry["name"], entry["file"]) for entry in snapshot["hotspots"]["classes"]),
                         [("Holder", "src/A/Holder.php"), ("Holder", "src/B/Holder.php"),
                          ("anonymous", "src/Two.php"), ("anonymous", "src/Two.php")])


class TypeCoverageTest(unittest.TestCase):
    def test_the_api_is_public_methods(self):
        coverage = snapshot_fields({
            "src/Api.php": """
                <?php
                class Api {
                  public $label;
                  public function names(string $prefix): string { return ''; }
                  protected function hidden($value) {}
                  private function secret($value): int { return 1; }
                  function implicit(int $value): Api { return $this; }
                }
            """,
        })["production"]["typeCoverage"]
        # names() and implicit(), public without saying so. A property is not a method; counted, it would
        # add an untyped return value.
        self.assertEqual(coverage, {"parameterTotal": 2, "parameterTyped": 2, "parameterPrecise": 2,
                                    "returnTotal": 2, "returnTyped": 2, "returnPrecise": 2})

    def test_constructors_and_destructors_have_no_return_value(self):
        coverage = snapshot_fields({
            "src/Counter.php": """
                <?php
                class Counter {
                  public function __construct(int $count) {}
                  public function __destruct() {}
                  public function count(): int { return 1; }
                }
            """,
        })["production"]["typeCoverage"]
        self.assertEqual(coverage, {"parameterTotal": 1, "parameterTyped": 1, "parameterPrecise": 1,
                                    "returnTotal": 1, "returnTyped": 1, "returnPrecise": 1})

    def test_a_type_is_imprecise_when_it_is_one_vague_type(self):
        coverage = snapshot_fields({
            "src/Api.php": """
                <?php
                class Api {
                  public function maybe(?array $items, int|string $key, array|false $flag, mixed $value, callable $callback,
                                        iterable $rows, object $entity): ?Api { return null; }
                  public function names(): array { return []; }
                }
            """,
        })["production"]["typeCoverage"]
        # Precise: int|string and array|false, which name more than one type, and ?Api. Imprecise, null
        # aside: array, mixed, callable, iterable and object alone.
        self.assertEqual(coverage, {"parameterTotal": 7, "parameterTyped": 7, "parameterPrecise": 2,
                                    "returnTotal": 2, "returnTyped": 2, "returnPrecise": 1})

    def test_methods_marked_as_hook_implementations_are_not_api(self):
        # Hook implementations arrived fully typed (761 in September 2026, none at 11.0.0) and are called by
        # the hook system, not by a reader of their signatures.
        coverage = snapshot_fields({
            "src/Hook/FooHooks.php": """
                <?php
                namespace Drupal\\foo\\Hook;

                use Drupal\\Core\\Hook\\Attribute\\Hook;
                use Drupal\\Core\\Hook\\Attribute\\Hook as HookAttribute;

                class FooHooks {
                  #[Hook('cron')]
                  public function cron(): void {}
                  #[HookAttribute('help')]
                  public function help($route): string { return ''; }
                  public function helper($value) {}
                  #[\\Other\\Hook('page_top')]
                  public function pageTop($page) {}
                }
            """,
        })["production"]["typeCoverage"]
        # The API is helper(), and pageTop(), whose attribute is another class named Hook.
        self.assertEqual(coverage, {"parameterTotal": 2, "parameterTyped": 0, "parameterPrecise": 0,
                                    "returnTotal": 2, "returnTyped": 0, "returnPrecise": 0})

    def test_a_class_level_hook_exempts_only_the_method_it_names(self):
        # A #[Hook] on a class names its method with the method argument, or means __invoke. The old
        # analyzer carried the exemption into the next class.
        coverage = snapshot_fields({
            "src/Hook/Hooks.php": """
                <?php
                namespace Drupal\\file\\Hook;

                use Drupal\\Core\\Hook\\Attribute\\Hook;

                #[Hook('file_download')]
                class FileDownloadHook {
                  public function __construct($file_system) {}
                  public function __invoke($uri): array { return []; }
                }
                #[Hook('cache_flush', method: 'flush')]
                class CacheFlushHook {
                  public function flush(): void {}
                }
                class CacheFlusher {
                  public function flush(): void {}
                }
            """,
        })["production"]["typeCoverage"]
        # The API is FileDownloadHook's constructor, which has no return value, and CacheFlusher::flush().
        self.assertEqual(coverage, {"parameterTotal": 1, "parameterTyped": 0, "parameterPrecise": 0,
                                    "returnTotal": 1, "returnTyped": 1, "returnPrecise": 1})


class DeprecationsTest(unittest.TestCase):
    def test_declarations_marked_deprecated_count_by_kind(self):
        deprecations = snapshot_fields({
            "src/Current.php": """
                <?php
                /**
                 * @deprecated in drupal:11.2.0 and is removed from drupal:12.0.0.
                 */
                class Old {}
                class Current {
                  /**
                   * @deprecated in drupal:10.1.0 and is removed from drupal:11.0.0.
                   */
                  public function old() {}
                  /**
                   * Replaces the method that was @deprecated in drupal:9.
                   */
                  public function replacement() {}
                  #[\\ReturnTypeWillChange]
                  public function legacy() {}
                  #[\\Deprecated]
                  public const OLD = 1;
                  /** @deprecated */
                  public $property;
                }
                /**
                 * @deprecated in drupal:10.3.0 and is removed from drupal:12.0.0.
                 */
                function old_function() {}
                /**
                 * @deprecated in drupal:11.1.0 and is removed from drupal:12.0.0.
                 */
                const OLD_GLOBAL = 1;
            """,
        })["deprecations"]
        # Deprecated: an @deprecated line in a docblock, with or without a version, or the #[\Deprecated]
        # attribute; OLD and OLD_GLOBAL are the two constants. Not deprecated: replacement(), whose docblock
        # mentions @deprecated in prose, and legacy(), which carries another attribute.
        self.assertEqual(deprecations, {"classes": 1, "methods": 1, "functions": 1, "properties": 1, "constants": 2,
                                        "services": 0, "libraries": 0})

    def test_services_and_libraries_marked_deprecated_count_outside_test_code(self):
        deprecations = snapshot_fields({
            "core.services.yml": """
                services:
                  old:
                    class: Old
                    deprecated: 'Gone'
            """,
            "core.libraries.yml": """
                old:
                  deprecated: 'Gone'
                  js: {}
            """,
            # Not counted: configuration schema marks keys deprecated too, and a testing profile defines services.
            "modules/node/config/schema/node.schema.yml": """
                node.settings:
                  type: config_object
                  deprecated: 'Gone'
            """,
            "profiles/testing/modules/foo_test/foo_test.services.yml": """
                services:
                  foo_test.thing:
                    class: Thing
                    deprecated: 'Gone'
            """,
        })["deprecations"]
        self.assertEqual(deprecations, {"classes": 0, "methods": 0, "functions": 0, "properties": 0, "constants": 0,
                                        "services": 1, "libraries": 1})


class SurfaceAreaTest(unittest.TestCase):
    def test_the_magic_key_vocabulary_is_property_names_set_in_literals(self):
        # The vocabulary is the magic keys set in array literals. Any key starting with # counted, including
        # regular expressions and stray strings: none at 10.0.0 and 16 in September 2026, growth that was
        # not vocabulary.
        snapshot = snapshot_fields({
            "src/Routes.php": """
                <?php
                class Routes {
                  function keys($form) {
                    // Set by an access, not in a literal.
                    $form['#weight_extra'] = 1;
                    return [
                      // Not property names: a regular expression, a key with a space, a placeholder.
                      '#entity.(?<entityTypeId>.+).canonical#' => 1,
                      '#required ' => 1,
                      '#@starterkit:label' => 1,
                      '#lazy_builder' => 1,
                      '#propsAlter' => 1,
                    ];
                  }
                }
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["magicKeys"], ["#lazy_builder", "#propsAlter"])

    def test_common_element_properties_are_not_vocabulary(self):
        # 28 common element properties are left out by name, a list kept by hand.
        snapshot = snapshot_fields({
            "modules/foo/foo.module": """
                <?php
                function foo_form() {
                  return [
                    'name' => ['#type' => 'textfield', '#title' => 'Name', '#description' => 'Your name.', '#required' => TRUE,
                               '#default_value' => '', '#size' => 60, '#maxlength' => 255, '#placeholder' => 'Name',
                               '#attributes' => [], '#weight' => 0, '#prefix' => '<div>', '#suffix' => '</div>',
                               '#disabled' => FALSE, '#id' => 'edit-name', '#name' => 'name', '#value' => ''],
                    'body' => ['#type' => 'textarea', '#rows' => 5, '#cols' => 60],
                    'count' => ['#type' => 'number', '#min' => 0, '#max' => 10, '#step' => 1],
                    'color' => ['#type' => 'select', '#options' => [], '#multiple' => FALSE, '#empty_option' => '- None -',
                                '#empty_value' => ''],
                    'help' => ['#markup' => 'Help', '#cache' => [], '#attached' => []],
                    'lazy' => ['#lazy_builder' => ['foo_lazy', []]],
                  ];
                }
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["magicKeys"], ["#lazy_builder"])

    def test_documented_hook_implementations_are_not_global_functions(self):
        # Implementations were recognized by guessing module names from file names, which missed .inc
        # files and every hook the analyzer failed to detect: 225 of 8.0.0's 840 global functions said
        # "Implements hook_" in their docblock. Drupal 6 wrote "Implementation of", sometimes leaving off
        # the hook's parentheses.
        snapshot = snapshot_fields({
            "modules/foo/foo.module": """
                <?php
                /**
                 * Implements hook_entity_type_alter().
                 */
                function foo_entity_type_alter(array &$entity_types) {}
                /**
                 * Implements MODULE_preprocess_HOOK().
                 */
                function foo_preprocess_node(&$variables) {}
                /**
                 * Implementation of hook_menu().
                 */
                function foo_menu() {}
                /**
                 * Implementation of hook_perm.
                 */
                function foo_perm() {}
                function foo_load($id) {}
            """,
            "modules/foo/foo.admin.inc": """
                <?php
                /**
                 * Implements hook_help().
                 */
                function foo_help() {}
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["foo_load"])

    def test_install_and_update_files_hold_no_global_functions(self):
        # Drupal calls every function in .install and .post_update.php files by name.
        snapshot = snapshot_fields({
            "modules/foo/foo.module": "<?php function foo_load() {}",
            "modules/foo/foo.install": "<?php function foo_schema() {} function foo_update_8001() {}",
            "modules/foo/foo.post_update.php": "<?php function foo_post_update_rename() {}",
        })
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["foo_load"])

    def test_template_preprocess_and_process_functions_are_not_global_functions(self):
        # Left out by name: Drupal calls template_preprocess_HOOK and template_process_HOOK functions, as
        # theme.api.php documents, and template_preprocess() and template_process() themselves.
        snapshot = snapshot_fields({
            "includes/theme.inc": """
                <?php
                function template_preprocess(&$variables, $hook) {}
                function template_process(&$variables, $hook) {}
                function template_preprocess_page(&$variables) {}
                function template_process_page(&$variables) {}
                function theme_get_registry() {}
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["theme_get_registry"])

    def test_functions_starting_with_an_underscore_are_internal(self):
        snapshot = snapshot_fields({"modules/foo/foo.module": "<?php function _foo_helper() {} function foo_api() {}"})
        self.assertEqual(snapshot["surfaceArea"]["globalFunctions"], ["foo_api"])

    def test_hooks_are_the_documented_ones(self):
        # Hooks were collected from invocations, #[Hook] attributes and docblocks, which counted a hook
        # invoked indirectly only once someone converted an implementation to OOP, and took a hand-kept
        # list of 17 patterns to leave out instances such as hook_node_insert.
        snapshot = snapshot_fields({
            "modules/node/node.api.php": """
                <?php
                function hook_node_access() {}
                function hook_ENTITY_TYPE_insert() {}
                function callback_batch_finished() {}
            """,
            # Not documentation: a hook_ function in a module, an invocation, and a test module's API file.
            "modules/node/node.module": "<?php function hook_node_example() { module_invoke_all('node_view_alter'); }",
            "modules/node/tests/node_test.api.php": """
                <?php
                function hook_node_test_only() {}
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["hooks"], ["hook_ENTITY_TYPE_insert", "hook_node_access"])

    def test_hook_documentation_counts_without_its_opening_php_tag(self):
        # help.api.php, the only file documenting hook_help in January 2015, had lost its opening
        # <?php tag, so PHP read it as HTML and declared nothing.
        snapshot = snapshot_fields({
            "modules/help/help.api.php": """
                /**
                 * Provide online user help.
                 */
                function hook_help($route_name) {}
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["hooks"], ["hook_help"])

    def test_plugin_types_are_classes_extending_the_default_plugin_manager(self):
        surface = snapshot_fields({
            "src/Managers.php": """
                <?php
                namespace Drupal\\foo;
                use Drupal\\Core\\Plugin\\DefaultPluginManager;
                class BlockManager extends DefaultPluginManager {}
                class FieldManager extends \\Drupal\\Core\\Plugin\\DefaultPluginManager {}
                class Other extends Base {}
            """,
        })["surfaceArea"]
        self.assertEqual(surface["pluginTypes"], ["BlockManager", "FieldManager"])

    def test_interface_methods_are_the_methods_of_interfaces(self):
        surface = snapshot_fields({
            "src/Entity.php": """
                <?php
                namespace Drupal\\foo;
                interface EntityInterface { const DEFAULT_ID = 'default'; public function save(); public function id(); }
                class Entity implements EntityInterface { public function save() {} public function id() {} }
            """,
        })["surfaceArea"]
        self.assertEqual(surface["interfaceMethods"], ["EntityInterface::id", "EntityInterface::save"])

    def test_events_are_the_keys_a_subscriber_subscribes_to(self):
        surface = snapshot_fields({
            "src/Subscriber.php": """
                <?php
                namespace Drupal\\foo;
                use Symfony\\Component\\EventDispatcher\\EventSubscriberInterface;
                use Symfony\\Component\\HttpKernel\\KernelEvents;
                class Subscriber implements EventSubscriberInterface {
                  public static function getSubscribedEvents(): array {
                    $events[KernelEvents::VIEW][] = ['onView'];
                    return $events + ['config.save' => 'onSave'];
                  }
                }
                class QualifiedSubscriber implements \\Symfony\\Component\\EventDispatcher\\EventSubscriberInterface {
                  public static function getSubscribedEvents() { return ['kernel.request' => 'onRequest']; }
                }
                class NotASubscriber implements \\Countable {
                  public static function getSubscribedEvents(): array { return ['not.an.event' => 'x']; }
                }
                class ComposerPlugin implements \\Composer\\EventDispatcher\\EventSubscriberInterface {
                  public static function getSubscribedEvents(): array { return ['post-install-cmd' => 'x']; }
                }
            """,
        })["surfaceArea"]
        # Keys as written, from Symfony's subscribers only: Composer has an EventSubscriberInterface of its own.
        self.assertEqual(surface["events"], ["KernelEvents::VIEW", "config.save", "kernel.request"])

    def test_service_types_are_the_prefixes_of_services_outside_test_code(self):
        # A service type is the part of a service's name before its first dot. A testing profile's services
        # counted as Core's when only /tests/ and /Tests/ were test code.
        snapshot = snapshot_fields({
            # Not counted: other.alias, a name pointing at another service, and a service named by its class.
            "core.services.yml": """
                services:
                  cache.default:
                    class: Cache
                  other.alias: '@cache.default'
                  Drupal\\Core\\Foo:
                    autowire: true
            """,
            "profiles/testing/modules/foo_test/foo_test.services.yml": """
                services:
                  foo_test.thing:
                    class: Thing
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["services"], ["cache"])


class EventsTest(unittest.TestCase):
    def test_a_subscriber_inheriting_the_interface_subscribes_too(self):
        # Twelve of Core's subscribers name EventSubscriberInterface nowhere: they extend a base class
        # that does, RouteSubscriberBase for most of them, and their events went uncounted.
        snapshot = snapshot_fields({
            "src/Routing/RouteSubscriberBase.php": """
                <?php
                namespace Drupal\\Core\\Routing;
                use Symfony\\Component\\EventDispatcher\\EventSubscriberInterface;
                abstract class RouteSubscriberBase implements EventSubscriberInterface {
                  public static function getSubscribedEvents() { return [RoutingEvents::ALTER => 'onAlterRoutes']; }
                }
            """,
            "modules/system/src/EventSubscriber/AdminRouteSubscriber.php": """
                <?php
                namespace Drupal\\system\\EventSubscriber;
                use Drupal\\Core\\Routing\\RouteSubscriberBase;
                class AdminRouteSubscriber extends RouteSubscriberBase {
                  public static function getSubscribedEvents() { return ['kernel.request' => 'onRequest']; }
                }
            """,
        })
        self.assertEqual(snapshot["surfaceArea"]["events"], ["RoutingEvents::ALTER", "kernel.request"])


class YamlFormatsTest(unittest.TestCase):
    # A format is read from a file's path, so the files are empty.

    def test_a_file_at_an_extension_root_is_named_extension_dot_format(self):
        # Detecting formats from discovery class names lost links.menu, links.task, links.action and
        # routing when Core renamed a class, and added schema to every snapshot, 2001's included.
        snapshot = snapshot_fields({
            "modules/node/node.info.yml": "",
            "modules/node/node.routing.yml": "",
            "modules/node/node.links.menu.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["info", "links.menu", "routing"])

    def test_a_file_below_an_extension_root_names_its_format_or_takes_its_directory(self):
        # <item>.<format>.yml takes the last part of its name; a plain .yml file takes the top directory
        # holding it. Detecting formats from discovery class names never found components.
        snapshot = snapshot_fields({
            "themes/olivero/olivero.info.yml": "",
            "themes/olivero/components/teaser/teaser.component.yml": "",
            "modules/layout/layout.info.yml": "",
            "modules/layout/layouts/static/one-col/one-col.yml": "",
            "modules/node/node.info.yml": "",
            "modules/node/migrations/d7_node.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["component", "info", "layouts", "migrations"])

    def test_configuration_is_data_except_its_schema(self):
        # Reading config/node.settings.yml as a format counted 137 configuration IDs in 2013, and
        # ckeditor5.data_types.yml read as a format named data_types.
        snapshot = snapshot_fields({
            "modules/node/node.info.yml": "",
            "modules/node/config/install/node.settings.yml": "",
            "modules/node/config/node.type.article.yml": "",
            "modules/ckeditor5/ckeditor5.info.yml": "",
            "modules/ckeditor5/config/schema/ckeditor5.data_types.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["info", "schema"])

    def test_yaml_outside_an_extension_is_not_a_format(self):
        # Core's own services and schema sit beside its modules, in no extension.
        snapshot = snapshot_fields({
            "core.services.yml": "",
            "config/schema/core.data_types.schema.yml": "",
            "modules/node/node.info.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["info"])

    def test_yaml_in_test_code_is_not_a_format(self):
        snapshot = snapshot_fields({
            "modules/node/node.info.yml": "",
            "modules/node/tests/modules/node_test/node_test.permissions.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["info"])

    def test_info_files_marked_extensions_before_yaml(self):
        # Until mid-2013 Drupal 8 modules declared themselves in .info files, so reading only
        # .info.yml put no YAML file inside an extension and January 2013 had no formats.
        snapshot = snapshot_fields({
            "modules/edit/edit.info": "",
            "modules/edit/edit.routing.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["routing"])

    def test_a_submodule_is_its_own_extension(self):
        # Search keeps search_node inside its own directory. Read from Search, the configuration object
        # search.page.node_search.yml sat under modules/, not config/, and read as a format named
        # node_search.
        snapshot = snapshot_fields({
            "modules/search/search.info.yml": "",
            "modules/search/modules/search_node/search_node.info.yml": "",
            "modules/search/modules/search_node/config/optional/search.page.node_search.yml": "",
        })
        self.assertEqual(snapshot["surfaceArea"]["yamlFormats"], ["info"])


class SnapshotIsolationTest(unittest.TestCase):
    def test_a_snapshot_reads_the_same_alone_as_beside_others(self):
        # Snapshots holding the same content share one set of facts, so a rule that stored anything on
        # them would carry one snapshot into the next: here the same file before and after Core moved
        # into core/.
        node = "<?php function node_load() {}"
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            before = commit_tree(repository, {"modules/node.module": node})
            after = commit_tree(repository, {"core/modules/node/node.module": node})
            together = facts.measure(repository, [before, after])
            alone = facts.measure(repository, [after])
        definitions.Snapshot(together[before]).fields()
        self.assertEqual(definitions.Snapshot(together[after]).fields(), definitions.Snapshot(alone[after]).fields())


if __name__ == "__main__":
    unittest.main()
