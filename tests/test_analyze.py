"""Behavior tests for analyze.py: the credit parser and the contributor series built on
it, commit classification, the git history walks (main line, snapshots, clone sync, credits
per issue), security advisories, initiative commits, page performance, source code age,
the fields data.json stores, and the guard against degraded writes.

Each test protects a behavior that was once broken in the wild or that a regression would
silently corrupt. Git behavior is tested against real repositories.

Run: python3 -m unittest discover tests
"""
import collections
import json
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import analyze as analyze_module  # noqa: E402
from analyze import (STRATEGIC_REPOS, CreditHistory, classify_commit,  # noqa: E402
                     extract_credits, get_contributors_per_year, get_half_of_credits_people,
                     get_most_credited, refuse_degraded_write, to_index_ranges)
from git_fixtures import commit, commit_change, git_history, init, merge, run_git  # noqa: E402
from test_definitions import measure_snapshot  # noqa: E402

# The runs these tests describe happen in 2026, so the latest complete year is 2025.
RUNNING_YEAR = 2026


class ExtractCreditsTest(unittest.TestCase):
    """The three credit conventions and their separator quirks."""

    def test_subject_convention(self):
        names = extract_credits("Issue #123 by alice, bob: Fix the thing", "")
        self.assertEqual(names, ["alice", "bob"])

    def test_security_convention_has_no_trailing_colon(self):
        names = extract_credits("SA-CORE-2026-009 by alice, bob", "")
        self.assertEqual(names, ["alice", "bob"])

    def test_trailer_convention_strips_at_prefix(self):
        # Unstripped @names split veterans into false first-timers (2025 bug:
        # firstTimeCredited was inflated 24%).
        names = extract_credits("fix: #9 Something", "fix: #9 Something\n\nBy: @alice\nBy: bob")
        self.assertEqual(names, ["alice", "bob"])

    def test_separators_comma_pipe_and(self):
        names = extract_credits("Issue #1 by alice | bob and carol, dave: x", "")
        self.assertEqual(names, ["alice", "bob", "carol", "dave"])

    def test_and_inside_a_name_does_not_split(self):
        self.assertEqual(extract_credits("Issue #1 by sandy: x", ""), ["sandy"])
        self.assertEqual(extract_credits("Issue #1 by b_and_w: x", ""), ["b_and_w"])

    def test_a_credit_list_holding_an_issue_reference_credits_only_the_people(self):
        # Committers have pasted the credit line into itself, so the list holds a second issue
        # reference and the separators split it out as a name: "Issue #2900112 by jibran" counted
        # as a person in 2017, and "#424372 by mr.baileys" in 2010. A person's name never carries
        # an issue reference; none of the 10,130 names in the full history holds a '#'.
        doubled = ("Issue #2900112 by jibran, mpdonadio, Issue #2900112 by jibran, mpdonadio: "
                   "Update non-Symfony dependencies")
        self.assertEqual(extract_credits(doubled, ""), ["jibran", "mpdonadio", "mpdonadio"])

    def test_uncredited_commit_yields_nothing(self):
        self.assertEqual(extract_credits("Back to dev.", "Back to dev."), [])

    def test_an_empty_trailer_does_not_credit_the_next_line(self):
        self.assertEqual(extract_credits("fix: #5 Something", "fix: #5 Something\n\nBy:\nSee the issue for details"), [])

    def test_revert_credits_differ_by_era(self):
        # Subject-era: the quoted subject still carries the credit list, so a
        # revert re-credits. Trailer-era: the quoted subject names nobody and
        # the revert body carries no "By:" trailers of its own, so it credits
        # nobody. Both behaviours are intended; see extract_credits.
        subject_era = 'Revert "Issue #1 by alice, bob: Do a thing"'
        self.assertEqual(extract_credits(subject_era, subject_era), ["alice", "bob"])

        trailer_era = 'Revert "task: #1 Do a thing"'
        body = f'{trailer_era}\n\nThis reverts commit abc123.\n'
        self.assertEqual(extract_credits(trailer_era, body), [])


class ClassifyCommitTest(unittest.TestCase):
    def test_a_conventional_prefix_sets_the_commit_type(self):
        self.assertEqual(classify_commit("fix: broken"), "bugs")
        self.assertEqual(classify_commit("feat: shiny"), "features")
        self.assertEqual(classify_commit("docs: words"), "maintenance")
        self.assertEqual(classify_commit("Issue #1 by alice: old style"), "uncategorized")


class SecurityAdvisoryTest(unittest.TestCase):
    """Advisory identifiers are keyed on (year, number) as integers, because early
    subjects zero-pad inconsistently and SA-CORE-2009-03 is the same advisory as
    SA-CORE-2009-003. Keying on the raw string counted 2009 and 2010 twice.

    The parsing tests call the module's own advisories_by_year: an earlier version
    asserted against a regex copy-pasted into the test, so it validated the copy rather
    than the module. The walk itself runs real git.
    """

    def advisories_from(self, subjects, running_year=None):
        """Counts by year. A test about parsing leaves the window alone, where it ends just past
        the newest advisory named; a test about the window itself names the year."""
        newest = max(int(year) for year, _ in analyze_module.ADVISORY.findall(subjects))
        return {row["year"]: row["count"]
                for row in analyze_module.advisories_by_year(subjects, running_year or newest + 1)}

    def test_the_running_year_is_left_out(self):
        # Part-elapsed, it understates its own count and reads as a change in the process.
        self.assertEqual(self.advisories_from("SA-CORE-2025-001\nSA-CORE-2026-002\n", running_year=2026), {2025: 1})

    def test_a_finished_year_without_an_advisory_is_still_drawn(self):
        # Every other series stores its empty periods (finished_months). A year missing from
        # this one leaves the chart with no bar at all rather than a quiet year, and the guard
        # before the write cannot catch it, because the series still grew.
        self.assertEqual(self.advisories_from("SA-CORE-2009-001\nSA-CORE-2011-001\n", running_year=2012),
                         {2009: 1, 2010: 0, 2011: 1})

    def test_an_advisory_reachable_only_from_a_tag_counts(self):
        # Core tagged security releases from commits no branch holds (four
        # SA-CORE-2025-004 release commits); the advisory still happened.
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [("main", "2025-01-01", "Issue #1 by alice: Start")])
            run_git(repository, "checkout", "--quiet", "--detach")
            commit_change(repository, "SA-CORE-2025-004 by bob", "2025-03-19")
            run_git(repository, "tag", "11.1.5")
            run_git(repository, "checkout", "--quiet", "main")
            advisories = analyze_module.get_security_advisories(repository, 2026)
        self.assertEqual(advisories, [{"year": 2025, "count": 1}])

    def test_zero_padding_variants_are_one_advisory(self):
        counts = self.advisories_from("Fix for SA-CORE-2009-03\n"
                                      "Rollback of SA-CORE-2009-003\n"
                                      "Unrelated SA-CORE-2009-005\n")
        self.assertEqual(counts, {2009: 2})

    def test_each_advisory_counts_once_however_many_commits_name_it(self):
        # A fix, a follow-up and a backport all naming one advisory is one advisory.
        counts = self.advisories_from("SA-CORE-2024-001 by alice\n"
                                      "Follow-up to SA-CORE-2024-001\n"
                                      "Backport of SA-CORE-2024-001\n")
        self.assertEqual(counts, {2024: 1})

    def test_advisories_are_grouped_by_their_own_year_not_the_commit_year(self):
        counts = self.advisories_from("SA-CORE-2023-004\nSA-CORE-2024-002\nSA-CORE-2024-003\n")
        self.assertEqual(counts, {2023: 1, 2024: 2})

    def test_a_failed_git_command_stops_counting_advisories(self):
        # An empty series would publish a chart with a hole in it.
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(analyze_module.GitError):
            analyze_module.get_security_advisories(Path(directory), 2026)

    def test_text_that_merely_resembles_an_advisory_is_not_counted(self):
        counts = self.advisories_from("Mentions SA-CONTRIB-2024-001 and sa-core-2024-002\n"
                                      "Real one: SA-CORE-2024-009\n")
        self.assertEqual(counts, {2024: 1})


def umami_history(directory: Path) -> Path:
    """A real git repository holding Umami's pinned metrics in both of the formats core has
    used: arrays in a test class, then one YAML file per scenario."""
    tests = directory / "core/profiles/demo_umami/tests/src/FunctionalJavascript"
    tests.mkdir(parents=True)
    init(directory)
    # CacheTagLookupQueryCount ends in QueryCount and must never be read as it.
    arrays = {"umamiNodePageColdCache": {"QueryCount": 458, "CacheTagLookupQueryCount": 43, "CacheSetCount": 441,
                                         "ScriptBytes": 12000},
              "umamiNodePageCoolCache": {"QueryCount": 191, "CacheGetCount": 210, "CacheSetCount": 65},
              "umamiFrontPageColdCache": {"QueryCount": 376, "CacheGetCount": 472, "CacheSetCount": 467}}
    (tests / "OpenTelemetryPerformanceTest.php").write_text("".join(
        "    $performance_data = $this->collectPerformanceData(function () {\n"
        "      $this->drupalGet('node/1');\n"
        f"    }}, '{label}');\n"
        "    $expected = [\n"
        + "".join(f"      '{metric}' => {value},\n" for metric, value in metrics.items())
        + "    ];\n"
        for label, metrics in arrays.items()))
    commit(directory, "Issue #3408713: Add database and cache assertions to the Umami performance tests", "2025-04-26")

    (tests / "OpenTelemetryPerformanceTest.php").write_text(
        "    }, 'umamiNodePageColdCache');\n    $this->assertMetricsByName('umamiNodePageColdCache', $performance_data);\n")
    assertions = tests / "OpenTelemetryPerformanceTestAssertions"
    assertions.mkdir()
    # CacheGetCountByBin breaks the reads down per cache bin and must never be read as CacheGetCount.
    (assertions / "umamiNodePageColdCache.yml").write_text(
        "QueryCount: 194\nCacheGetCount: 221\nCacheGetCountByBin:\n  config: 53\nCacheSetCount: 229\n"
        "CacheTagLookupQueryCount: 25\nScriptBytes: 12091\nStylesheetBytes: 39430\n")
    (assertions / "umamiNodePageCoolCache.yml").write_text("QueryCount: 61\nCacheGetCount: 164\nCacheSetCount: 58\n")
    (assertions / "umamiFrontPageColdCache.yml").write_text("QueryCount: 178\nCacheGetCount: 225\nCacheSetCount: 240\n")
    (assertions / "administratorNodePage.yml").write_text("QueryCount: 264\nScriptBytes: 187160\nStylesheetBytes: 73614\n")
    commit(directory, "task: #3618432 Move expected metrics and queries to files", "2026-08-25")
    return directory


class PagePerformanceTest(unittest.TestCase):
    """The metrics core's performance tests pin for Umami's pages, read from git alone,
    across both places core has kept them: arrays in the test classes, then one YAML file
    per scenario."""

    TESTS = "core/profiles/demo_umami/tests/src/FunctionalJavascript/"

    def php(self, commit, scenarios):
        """git grep output for a test class pinning {label: {metric: value}}."""
        path = f"{commit}:{self.TESTS}PerformanceTest.php:"
        lines = []
        for label, metrics in scenarios.items():
            lines.append(f"{path}    $performance_data = $this->collectPerformanceData(function () {{")
            lines.append(f"{path}    }}, '{label}');")
            lines.extend(f"{path}      '{metric}' => {value}," for metric, value in metrics.items())
        return "".join(line + "\n" for line in lines)

    def yaml(self, commit, scenarios):
        """git grep output for one YAML file per scenario pinning {label: {metric: value}}."""
        return "".join(f"{commit}:{self.TESTS}PerformanceTestAssertions/{label}.yml:{metric}: {value}\n"
                       for label, metrics in scenarios.items() for metric, value in metrics.items())

    def visitor_queries(self, article_cold_cache, article_cool_cache, front_page_cold_cache):
        return {"umamiNodePageColdCache": {"QueryCount": article_cold_cache},
                "umamiNodePageCoolCache": {"QueryCount": article_cool_cache},
                "umamiFrontPageColdCache": {"QueryCount": front_page_cold_cache}}

    def column(self, rows, scenario, metric="QueryCount"):
        return [row["metrics"][scenario][metric] for row in rows]

    def rows_from(self, commits, expectations, subjects=None):
        """commits: [(sha, date)], oldest first; expectations: {sha: git grep output};
        subjects: {sha: commit subject}, for the commits whose subject matters."""
        noon = lambda date: int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 43200
        log = "".join(f"{sha}\x1f{noon(date)}\x1f{(subjects or {}).get(sha, 'Update the performance tests')}\n"
                      for sha, date in reversed(commits))

        def fake_git(repository, *arguments, accepted_exit_codes=(0,)):
            if arguments[0] == "symbolic-ref":
                return "main\n"
            if arguments[0] == "log":
                return "" if "--merges" in arguments else log
            return expectations[arguments[arguments.index("--") - 1]]
        with unittest.mock.patch.object(analyze_module, "git", fake_git):
            return analyze_module.get_page_performance(Path("drupal-core"))

    def test_the_real_git_commands_read_both_formats(self):
        # Everything else here fakes git's output; this runs the actual log and grep, so the
        # parsing path is exercised against a real repository. It does not catch every broken
        # command line: a pathspec that stops limiting leaves this green, because
        # read_pinned_metrics limits again downstream. Measured, not assumed.
        with tempfile.TemporaryDirectory() as directory:
            repository = umami_history(Path(directory))
            commits = run_git(repository, "rev-list", "--reverse", "HEAD").split()
            rows = analyze_module.get_page_performance(repository)
        self.assertEqual(rows, [
            {"date": "2025-04-26", "commit": commits[0], "issue": 3408713, "metrics": {
                "umamiNodePageColdCache": {"QueryCount": 458, "CacheGetCount": None, "CacheSetCount": 441,
                                           "ScriptBytes": 12000, "StylesheetBytes": None},
                "umamiNodePageCoolCache": {"QueryCount": 191, "CacheGetCount": 210, "CacheSetCount": 65},
                "umamiFrontPageColdCache": {"QueryCount": 376, "CacheGetCount": 472, "CacheSetCount": 467},
                "administratorNodePage": {"QueryCount": None, "ScriptBytes": None, "StylesheetBytes": None},
                "umamiFrontAndRecipePages": {"ScriptBytes": None, "StylesheetBytes": None},
                "umamiFrontAndRecipePagesAuthenticated": {"ScriptBytes": None, "StylesheetBytes": None}}},
            {"date": "2026-08-25", "commit": commits[1], "issue": 3618432, "metrics": {
                "umamiNodePageColdCache": {"QueryCount": 194, "CacheGetCount": 221, "CacheSetCount": 229,
                                           "ScriptBytes": 12091, "StylesheetBytes": 39430},
                "umamiNodePageCoolCache": {"QueryCount": 61, "CacheGetCount": 164, "CacheSetCount": 58},
                "umamiFrontPageColdCache": {"QueryCount": 178, "CacheGetCount": 225, "CacheSetCount": 240},
                "administratorNodePage": {"QueryCount": 264, "ScriptBytes": 187160, "StylesheetBytes": 73614},
                "umamiFrontAndRecipePages": {"ScriptBytes": None, "StylesheetBytes": None},
                "umamiFrontAndRecipePagesAuthenticated": {"ScriptBytes": None, "StylesheetBytes": None}}}])

    def test_a_merged_branch_counts_as_its_merge(self):
        # An unsquashed branch passes through pinned values that never stood on core's
        # main line; only the merge did.
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            assertions = repository / (self.TESTS + "OpenTelemetryPerformanceTestAssertions")
            assertions.mkdir(parents=True)

            def pin(queries, date):
                (assertions / "umamiNodePageColdCache.yml").write_text(f"QueryCount: {queries}\n")
                commit(repository, date, date)

            init(repository)
            pin(458, "2026-01-10")
            run_git(repository, "checkout", "--quiet", "-b", "sandbox")
            pin(350, "2026-02-10")
            pin(300, "2026-02-20")
            run_git(repository, "checkout", "--quiet", "main")
            merge(repository, "sandbox", "Merge the sandbox", "2026-03-01")
            rows = analyze_module.get_page_performance(repository)
        self.assertEqual([row["date"] for row in rows], ["2026-01-10", "2026-03-01"])
        self.assertEqual(self.column(rows, "umamiNodePageColdCache"), [458, 300])

    def test_a_metric_is_null_until_core_first_pins_it(self):
        # Rows begin with the first pinned value; each chart then starts where all of its
        # own lines are pinned.
        rows = self.rows_from(
            [("a", "2025-01-01"), ("b", "2025-04-26")],
            {"a": self.php("a", {"umamiNodePageCoolCache": {"QueryCount": 190}}),
             "b": self.php("b", self.visitor_queries(458, 191, 376))})
        self.assertEqual(self.column(rows, "umamiNodePageCoolCache"), [190, 191])
        self.assertEqual(self.column(rows, "umamiNodePageColdCache"), [None, 458])

    def test_a_row_is_added_only_when_a_value_changes(self):
        same = self.visitor_queries(458, 191, 376)
        rows = self.rows_from([("a", "2025-04-26"), ("b", "2025-05-01")],
                              {"a": self.php("a", same), "b": self.php("b", same)})
        self.assertEqual(len(rows), 1)

    def test_each_row_names_the_commit_and_issue_that_recorded_it(self):
        # The dashboard links every step to both. core's subjects name the issue as
        # "Issue #N" and, since November 2025, as "type: #N"; a subject naming none has no issue.
        rows = self.rows_from(
            [("a", "2025-04-26"), ("b", "2026-02-03"), ("c", "2026-03-02")],
            {"a": self.php("a", self.visitor_queries(458, 191, 376)),
             "b": self.yaml("b", self.visitor_queries(225, 77, 205)),
             "c": self.yaml("c", self.visitor_queries(224, 77, 205))},
            subjects={"a": "Issue #3408713: Add database and cache assertions",
                      "b": "perf: #3564937 Remove duplicative caching of views rows",
                      "c": "Update the performance tests"})
        self.assertEqual([(row["commit"], row["issue"]) for row in rows], [("a", 3408713), ("b", 3564937), ("c", None)])

    def test_a_scenario_that_disappears_ends_as_null(self):
        rows = self.rows_from([("a", "2025-04-26"), ("b", "2027-01-01")],
                              {"a": self.php("a", self.visitor_queries(458, 191, 376)), "b": ""})
        self.assertEqual(self.column(rows, "umamiNodePageColdCache"), [458, None])
        self.assertEqual(self.column(rows, "umamiFrontPageColdCache"), [376, None])

    def test_a_scenario_dropped_and_then_restored_keeps_its_values(self):
        # A commit that drops a label and a revert that restores it must not end the
        # line for good; the gap is recorded and the chart holds the line across it.
        rows = self.rows_from(
            [("a", "2025-04-26"), ("b", "2026-01-01"), ("c", "2026-01-02")],
            {"a": self.php("a", self.visitor_queries(458, 191, 376)),
             "b": self.php("b", {"umamiNodePageColdCache": {"QueryCount": 458},
                                 "umamiNodePageCoolCache": {"QueryCount": 191}}),
             "c": self.php("c", self.visitor_queries(458, 191, 376))})
        self.assertEqual(self.column(rows, "umamiFrontPageColdCache"), [376, None, 376])

    def test_numbers_after_an_unnamed_scenario_are_not_misattributed(self):
        # Neither a label passed as a variable nor a collection with no label may hand its
        # numbers to the literal label before it.
        path = f"a:{self.TESTS}PerformanceTest.php:"
        output = (self.php("a", self.visitor_queries(458, 191, 376))
                  + f"{path}    }}, $label);\n"
                  + f"{path}      'QueryCount' => 999,\n"
                  + f"{path}    }}, 'administratorNodePage');\n"
                  + f"{path}      'QueryCount' => 264,\n"
                  + f"{path}    $this->collectPerformanceData(function () {{\n"
                  + f"{path}      'QueryCount' => 999,\n")
        rows = self.rows_from([("a", "2025-04-26")], {"a": output})
        self.assertEqual(self.column(rows, "umamiFrontPageColdCache"), [376])
        self.assertEqual(self.column(rows, "administratorNodePage"), [264])

    def test_a_failed_git_command_stops_the_run(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(analyze_module.GitError):
            analyze_module.get_page_performance(Path(directory))

    def test_a_failed_grep_stops_the_run(self):
        # Reading the pinned numbers returned nothing when git grep failed, and the whole
        # series came back empty instead of stopping.
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [("main", "2024-01-01", "Start")])
            with self.assertRaises(analyze_module.GitError):
                analyze_module.read_pinned_metrics(repository, "0" * 40)


class SurfaceAreaIndexTest(unittest.TestCase):
    """Each surface-area item is stored once, with the snapshot index ranges it exists in."""

    def test_collapses_contiguous_runs(self):
        self.assertEqual(to_index_ranges([0, 1, 2, 5, 7, 8]), [[0, 2], [5, 5], [7, 8]])

    def test_lists_become_index_ranges_and_the_snapshots_are_left_alone(self):
        snapshots = [{"date": "2020-01", "surfaceArea": {"hooks": ["hook_a"]}},
                     {"date": "2020-07", "surfaceArea": {"hooks": ["hook_a", "hook_b"]}}]
        separated, index = analyze_module.separate_surface_area(snapshots)
        self.assertEqual(separated, [{"date": "2020-01"}, {"date": "2020-07"}])
        self.assertEqual(index, {"hooks": {"hook_a": [[0, 1]], "hook_b": [[1, 1]]}})
        self.assertIn("surfaceArea", snapshots[0])


class PlanSnapshotsTest(unittest.TestCase):
    """Snapshots fall at the start of every January and July, then HEAD. Source code age dates
    the last snapshot of each past year and this year's HEAD."""

    @staticmethod
    def landed(*dates):
        commits = [analyze_module.LandedCommit(
            f"commit-{date}", int(datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()), "Change")
            for date in dates]
        return sorted(commits, key=lambda landed_commit: landed_commit.timestamp, reverse=True)

    def test_every_january_and_july_then_head(self):
        planned = analyze_module.plan_snapshots(self.landed("1999-12-20", "2025-06-15", "2025-12-20", "2026-08-01"),
                                                "head", datetime(2026, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(len(planned), 54 + 1)
        self.assertEqual(planned[-4:], [("2025-07", "commit-2025-06-15", True), ("2026-01", "commit-2025-12-20", False),
                                        ("2026-07", "commit-2025-12-20", False), ("2026-09", "head", True)])

    def test_the_last_snapshot_stands_for_head_in_its_own_month(self):
        planned = analyze_module.plan_snapshots(self.landed("2026-01-20"), "head", datetime(2026, 7, 14, tzinfo=timezone.utc))
        self.assertEqual(planned, [("2026-07", "commit-2026-01-20", True)])

    def test_months_before_the_first_commit_are_skipped(self):
        planned = analyze_module.plan_snapshots(self.landed("2025-03-01"), "head", datetime(2025, 9, 14, tzinfo=timezone.utc))
        self.assertEqual(planned, [("2025-07", "commit-2025-03-01", False), ("2025-09", "head", True)])


def synthetic_history(events):
    """Build a CreditHistory from {year: [names...]} without touching git."""
    names_by_year, counts_by_year, first_seen = {}, {}, {}
    for year, names in events.items():
        names_by_year[year] = set(names)
        counts_by_year[year] = collections.Counter(names)
        for name in names:
            if name not in first_seen or year < first_seen[name]:
                first_seen[name] = year
    return CreditHistory(names_by_year, counts_by_year, first_seen)


class ContributorSeriesTest(unittest.TestCase):
    def test_partial_years_are_not_emitted(self):
        year = RUNNING_YEAR - 1
        history = synthetic_history({
            year - 4: ["alice", "bob"],
            year - 3: ["alice"],
            year - 2: ["alice", "carol"],
            year - 1: ["alice"],
            year: ["alice", "dave"],
            year + 1: ["alice"],                # the running year, still partial
        })
        years = [row["year"] for row in get_contributors_per_year(history, RUNNING_YEAR)]
        self.assertEqual(years[-1], year)

    def test_credits_by_first_year_account_for_every_credit(self):
        # Every credit of the year must land in the first-credit year of the
        # person who earned it.
        year = RUNNING_YEAR - 1
        history = synthetic_history({
            year - 3: ["veteran"],
            year: ["veteran", "veteran", "veteran", "newcomer"],
        })
        row = [r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year][0]
        self.assertEqual(row["creditsByFirstYear"], {str(year - 3): 3, str(year): 1})


class MostCreditedTest(unittest.TestCase):
    def test_every_generation_is_named_with_the_year_each_person_arrived(self):
        # The table draws the newer end of this list, and the field carries the whole of it, each
        # person with the year they were first credited, so the chart bands them as it likes.
        year = RUNNING_YEAR - 1
        history = synthetic_history({
            year - 10: ["veteran"],
            year - 2: ["newcomer"],
            year: ["veteran", "newcomer"],
        })
        credited = get_most_credited(history, RUNNING_YEAR)
        self.assertEqual(credited["year"], year)
        self.assertEqual(credited["people"], [{"name": "newcomer", "firstCredited": year - 2},
                                              {"name": "veteran", "firstCredited": year - 10}])

    def test_people_tied_with_the_last_place_are_named_too(self):
        # most_common() breaks a tie in the order the credit history was walked, so who came last
        # depended on the walk rather than on the credits.
        year = RUNNING_YEAR - 1
        busy = [f"busy{number:02}" for number in range(analyze_module.CORE_TIER_SIZE - 1)]
        tied = ["tied_a", "tied_b", "tied_c"]
        history = synthetic_history({year: busy * 5 + tied * 2})
        names = [person["name"] for person in get_most_credited(history, RUNNING_YEAR)["people"]]
        self.assertEqual(sorted(name for name in names if name.startswith("tied")), tied)
        self.assertEqual(len(names), len(busy) + len(tied))


class HalfOfCreditsTest(unittest.TestCase):
    """The people with half the credits appear twice, as the charted count and as
    the names under it, and both must describe the same group."""

    def test_count_and_names_agree(self):
        year = RUNNING_YEAR - 1
        history = synthetic_history({
            year - 8: ["zed"],
            year - 1: ["bob"],
            year: ["zed"] * 4 + ["bob"] * 3 + ["alice"] * 3,
        })
        row = [r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year][0]
        people = get_half_of_credits_people(history, RUNNING_YEAR)
        names = [person["name"] for person in people["people"]]
        self.assertEqual(row["halfOfCredits"], 2)
        self.assertEqual(names, ["bob", "zed"])
        self.assertEqual(set(people["people"][0]), {"name", "firstCredited"})

    def test_people_who_asked_not_to_be_listed_are_counted_but_not_named(self):
        unlisted = next(iter(analyze_module.NOT_LISTED_BY_NAME))
        year = RUNNING_YEAR - 1
        history = synthetic_history({year - 1: [unlisted, "newcomer"],
                                     year: [unlisted] * 5 + ["newcomer"] * 4})
        row = [r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year][0]
        self.assertEqual(row["halfOfCredits"], 1)
        half = get_half_of_credits_people(history, RUNNING_YEAR)
        self.assertEqual((half["people"], half["unlisted"]), ([], 1))
        # Both name lists withhold the same people, because both are built by listed_people: the
        # most-credited list names the other person and counts the one left out.
        credited = get_most_credited(history, RUNNING_YEAR)
        self.assertEqual((credited["people"], credited["unlisted"]),
                         ([{"name": "newcomer", "firstCredited": year - 1}], 1))


class CloneSyncTest(unittest.TestCase):
    """A clone stays level with its remote: the same branches at the same commits, and
    HEAD on the remote's default branch. A plain fetch into a bare clone moved nothing,
    and HEAD was only ever set when cloning, so a repository cloned while empty never
    showed a commit."""

    def test_a_clone_made_while_empty_follows_its_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, clone = Path(directory, "remote"), Path(directory, "clone.git")
            init(remote)
            analyze_module.sync_clone(str(remote), clone)
            git_history(remote, [("main", "2024-01-01", "Start"), ("1.x", "2025-01-01", "Release")])
            run_git(remote, "symbolic-ref", "HEAD", "refs/heads/1.x")
            analyze_module.sync_clone(str(remote), clone)

            def state(repository):
                return [run_git(repository, *command) for command in (["symbolic-ref", "HEAD"], ["for-each-ref", "refs/heads"])]
            self.assertEqual(state(clone), state(remote))


class CloneSyncFailureModesTest(unittest.TestCase):
    """What a remote can do between two daily runs: move a tag, delete a tag or a branch.
    --tags refused to move a re-pointed tag and failed every run after."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        remote = Path(directory.name, "remote")
        remote.mkdir()
        self.remote = git_history(remote, [("main", "2024-01-01", "Start")])
        self.clone = Path(directory.name, "clone.git")

    def refs(self, repository):
        return run_git(repository, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/tags")

    def sync(self):
        analyze_module.sync_clone(str(self.remote), self.clone)

    def test_a_tag_moved_on_the_remote_moves_in_the_clone(self):
        run_git(self.remote, "tag", "1.0.0")
        self.sync()
        commit_change(self.remote, "Retagged release", "2024-02-01")
        run_git(self.remote, "tag", "--force", "1.0.0")
        self.sync()
        self.assertEqual(self.refs(self.clone), self.refs(self.remote))

    def test_tags_and_branches_deleted_on_the_remote_are_deleted_in_the_clone(self):
        run_git(self.remote, "tag", "1.0.0")
        run_git(self.remote, "branch", "sandbox")
        self.sync()
        run_git(self.remote, "tag", "--delete", "1.0.0")
        run_git(self.remote, "branch", "--delete", "sandbox")
        self.sync()
        self.assertEqual(self.refs(self.clone), self.refs(self.remote))


class FailuresStopTheRunTest(unittest.TestCase):
    """A failure that only warned let the run publish: a stale clone stamped as updated
    today, or source code age dating fewer lines than it counted."""

    def test_an_update_that_cannot_reach_the_remote_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory, "remote")
            remote.mkdir()
            git_history(remote, [("main", "2024-01-01", "Start")])
            clone = Path(directory, "drupal-core")
            with unittest.mock.patch.object(analyze_module, "DRUPAL_REPO_URL", str(remote)):
                self.assertTrue(analyze_module.setup_drupal(clone))
                shutil.rmtree(remote)
                self.assertFalse(analyze_module.setup_drupal(clone))

    def test_a_blame_that_fails_stops_the_run(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [("main", "2024-01-01", "Start")])
            with self.assertRaises(analyze_module.GitError):
                analyze_module.date_code_lines(repository, "HEAD", {"missing.php": [1]})


class SourceCodeAgeTest(unittest.TestCase):
    """Source code age dates exactly the lines the analyzer counts as production code.
    It used to rebuild that selection from its own copy of the rules, which drifted
    whenever one side changed and was only noticed by a warning after a full run."""

    def test_counted_lines_are_dated_by_the_commit_that_last_changed_them(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory, "drupal")
            code = repository / "core/lib/Drupal/Core"
            code.mkdir(parents=True)
            init(repository)
            (code / "Cron.php").write_text("<?php\nclass Cron {\n}\n")
            commit(repository, "2019-03-01", "2019-03-01")
            (code / "Cron.php").write_text("<?php\nclass Cron {\n  public function run() {\n  }\n}\n")
            (code / "CronTest.php").write_text("<?php\nclass CronTest {\n}\n")
            (code / "ProxyClass").mkdir()
            (code / "ProxyClass/Cron.php").write_text("<?php\n/**\n * This file was generated via a script.\n */\nclass Cron {\n}\n")
            head = commit(repository, "2024-03-01", "2024-03-01")

            (snapshot,), (age,) = analyze_module.analyze_snapshots(repository, [("2024-07", head, True)])
        self.assertEqual(age, {"year": 2024, "date": "2024-07", "lastTouched": {"2019": 3, "2024": 2}})
        self.assertEqual(sum(age["lastTouched"].values()), snapshot["production"]["lines"])


class CeremonialCommitTest(unittest.TestCase):
    """One 2017 commit credited 1,661 people for a three-line MAINTAINERS.txt
    change, duplicating credits already granted on the real issues. CREDIT_LIST_LIMIT
    drops those, and the guard lived in collect_credit_history with no coverage: a
    regression would silently re-inflate a year's contributor count.
    """

    def history_for(self, subjects):
        with tempfile.TemporaryDirectory() as directory:
            return analyze_module.collect_credit_history(
                git_history(Path(directory), [("main", "2024-06-01", subject) for subject in subjects]))

    def test_an_oversized_credit_list_is_dropped_entirely(self):
        crowd = ", ".join(f"person{i}" for i in range(analyze_module.CREDIT_LIST_LIMIT + 1))
        history = self.history_for([f"Issue #1 by {crowd}: Thank everyone"])
        self.assertEqual(history.names_by_year.get(2024, set()), set())

    def test_a_large_but_plausible_credit_list_is_kept(self):
        # The largest genuine single-issue list in the record is 126 names.
        crowd = ", ".join(f"person{i}" for i in range(126))
        history = self.history_for([f"Issue #2 by {crowd}: A real issue"])
        self.assertEqual(len(history.names_by_year[2024]), 126)

    def test_dropping_a_ceremonial_commit_does_not_drop_its_people_elsewhere(self):
        crowd = ", ".join(f"person{i}" for i in range(analyze_module.CREDIT_LIST_LIMIT + 1))
        history = self.history_for([
            f"Issue #1 by {crowd}: Thank everyone",
            "Issue #2 by person5, person9: A real fix",
        ])
        self.assertEqual(history.names_by_year[2024], {"person5", "person9"})


class ConcentrationTest(unittest.TestCase):
    """halfOfCredits answers "how few people carry half the work", which headcount
    cannot: people credited fell from 1,346 in 2016 to 1,118 in 2025, while it fell
    from 57 to 19.
    """

    def test_half_of_credits_counts_the_smallest_group_reaching_half(self):
        year = RUNNING_YEAR - 2
        # One person with 10 credits, then four with 1 each: 10 of 14 is already
        # past half, so the answer is 1, not 3.
        history = synthetic_history({year: ["heavy"] * 10 + ["a", "b", "c", "d"]})
        row = next(r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year)
        self.assertEqual(row["halfOfCredits"], 1)

    def test_evenly_shared_work_needs_half_the_people(self):
        year = RUNNING_YEAR - 2
        history = synthetic_history({year: ["a", "b", "c", "d"]})
        row = next(r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year)
        self.assertEqual(row["halfOfCredits"], 2)


class StoredFieldsAreDrawnTest(unittest.TestCase):
    """Every stored field must be referenced by the dashboard.

    The rule is that data.json carries only fields a chart draws, and the
    rule had already been broken three times before anyone checked: poolTenure
    outlived the chart that justified it, and halfOfCredits and firstTimersRetained
    (since removed) were computed every run and never read. A test is cheaper
    than an audit.
    """

    def setUp(self):
        self.html = (Path(__file__).parent.parent / "index.html").read_text()

    def assertIndexHtmlDraws(self, fields: dict, holder: str = ""):
        """Every field of a stored object reaches a chart, and so does every field of the objects and
        list rows inside it. A field reaches a chart when the dashboard names it, or when a chart takes
        every value the object holding it carries: iterating it (Object.values(entry.deprecations)) or
        indexing it by a key it builds (typeCoverage[dimension + 'Typed']). Object.keys() is not one of
        them: reading a holder's names, as a presence check does, draws none of its values."""
        whole = (r"Object\.(values|entries)\([\w.?\[\]]*\b" + re.escape(holder) + r"\b(\s*\|\|\s*\{\})?\s*\)"
                 r"|\b" + re.escape(holder) + r"(\?\.)?\[[^\]'\"0-9]")
        if holder and re.search(whole, self.html):
            return
        for field, value in fields.items():
            # Matched as a property access (`.field`, `['field']`, `"field"`, and inside a template
            # literal's `${...}`) rather than as a bare substring: "count" and "year" occur all over a
            # file this size, so a plain `in` check passed for fields nothing actually read.
            pattern = re.compile(r"[.\[]\s*['\"]?" + re.escape(field) + r"['\"]?\s*[\]\s,;.)(?}]")
            with self.subTest(field=field):
                self.assertTrue(
                    pattern.search(self.html),
                    f"{field} is stored but index.html never reads it as a property; "
                    "draw it or stop storing it")
            row = value[0] if isinstance(value, list) and value else value
            if isinstance(row, dict):
                self.assertIndexHtmlDraws(row, field)

    def test_every_contributor_field_appears_in_index_html(self):
        year = RUNNING_YEAR - 2
        history = synthetic_history({year - 1: ["a"], year: ["a", "b"]})
        row = next(r for r in get_contributors_per_year(history, RUNNING_YEAR) if r["year"] == year)
        self.assertIndexHtmlDraws(row)

    def test_every_name_list_field_appears_in_index_html(self):
        year = RUNNING_YEAR - 1
        history = synthetic_history({year - 1: ["alice"], year: ["alice", "alice", "bob"]})
        for name_list in (get_most_credited(history, RUNNING_YEAR), get_half_of_credits_people(history, RUNNING_YEAR)):
            self.assertIndexHtmlDraws(name_list)

    def test_every_page_performance_field_appears_in_index_html(self):
        with tempfile.TemporaryDirectory() as directory:
            row = analyze_module.get_page_performance(umami_history(Path(directory)))[-1]
        self.assertIndexHtmlDraws(row)

    def test_every_codebase_field_appears_in_index_html(self):
        # A tree small enough to read, holding one of each thing a snapshot counts: a class with a
        # method, so the hotspot rows are there to check, and an extension's YAML.
        fields = measure_snapshot({
            "src/Example.php": """
                <?php
                class Example {
                  public function run(array $items) { if ($items) { return $this->run([]); } return NULL; }
                }
            """,
            "example.services.yml": "services:\n  example:\n    class: Example\n",
        }).fields()
        self.assertIndexHtmlDraws(fields)

    def test_every_page_performance_field_index_html_reads_is_stored(self):
        # The other direction: a typo in an accessor draws no card at all, because the
        # chart never finds a row where all of its lines are pinned.
        html = (Path(__file__).parent.parent / "index.html").read_text()
        scenarios = set(re.findall(r"\.metrics\.(\w+)", html))
        metrics = set(re.findall(r"\.([A-Z]\w+(?:Count|Bytes))\b", html))
        self.assertTrue(scenarios and metrics, "no page performance accessors found in index.html")
        self.assertEqual(scenarios - set(analyze_module.PAGE_PERFORMANCE_METRICS), set())
        self.assertEqual(metrics - set(analyze_module.PINNED_METRICS), set())


class RefuseDegradedWriteTest(unittest.TestCase):
    """A failed git command stops the run, but a collection can still come back empty
    or shorter for other reasons, so the guard before the write stands between that
    and a silently gutted public dashboard.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_file = Path(self.directory.name) / "data.json"
        self.good = {
            "monthlyCommits": [{"date": "2025-01"}, {"date": "2025-02"}],
            "contributors": [{"year": 2024}, {"year": 2025}],
            "initiatives": [{"date": "2025-01"}, {"date": "2025-02"}],
            "pagePerformance": [{"date": "2025-04-26"}, {"date": "2025-05-09"}],
            "securityAdvisories": [{"year": 2024}, {"year": 2025}],
            "snapshots": [{"date": "2025-01"}, {"date": "2025-07"}],
            "sourceCodeAge": [{"year": 2024}, {"year": 2025}],
            "mostCredited": {"year": 2025, "people": []},
            "halfOfCreditsPeople": {"year": 2025, "people": []},
            "surfaceArea": {"hooks": {"hook_help": [[0, 1]]}},
        }
        self.data_file.write_text(json.dumps(self.good))

    def write(self, data, allow_shrink=False):
        refuse_degraded_write(data, self.data_file, allow_shrink)

    def test_fixture_covers_every_guarded_series(self):
        # Adding a series to APPEND_ONLY_SERIES without adding it here makes every
        # other test in this class fail for the wrong reason: the guard aborts on
        # the missing key rather than on the behaviour under test. `initiatives`
        # was added to the guard and did exactly that.
        from analyze import APPEND_ONLY_SERIES
        for name in APPEND_ONLY_SERIES:
            self.assertIn(name, self.good, f"{name} is guarded but missing from the fixture")

    def test_unchanged_and_grown_data_are_not_refused(self):
        self.write(self.good)
        grown = dict(self.good, monthlyCommits=self.good["monthlyCommits"] + [{"date": "2025-03"}])
        self.write(grown)

    def test_empty_series_aborts(self):
        from analyze import APPEND_ONLY_SERIES
        for name in APPEND_ONLY_SERIES:
            with self.subTest(series=name):
                with self.assertRaises(SystemExit) as caught:
                    self.write(dict(self.good, **{name: []}))
                self.assertEqual(caught.exception.code, 1)

    def test_empty_name_tables_or_surface_area_abort(self):
        for name in ("mostCredited", "halfOfCreditsPeople", "surfaceArea"):
            with self.subTest(field=name), self.assertRaises(SystemExit):
                self.write(dict(self.good, **{name: {}}))

    def test_missing_series_aborts(self):
        without = {k: v for k, v in self.good.items() if k != "contributors"}
        with self.assertRaises(SystemExit):
            self.write(without)

    def test_an_unreadable_existing_file_stops_the_run(self):
        # The guard used to warn and then write past its own shrink check here, in the one
        # case where the published file is already known to be wrong.
        self.data_file.write_text("{ not json")
        with self.assertRaises(json.JSONDecodeError):
            self.write(self.good)

    def test_shrunk_series_aborts_unless_explicitly_allowed(self):
        shrunk = dict(self.good, contributors=self.good["contributors"][:1])
        with self.assertRaises(SystemExit):
            self.write(shrunk)
        self.write(shrunk, allow_shrink=True)

    def test_first_ever_run_has_nothing_to_compare_against(self):
        self.data_file.unlink()
        self.write(self.good)


class InitiativeCommitsTest(unittest.TestCase):
    """Where the initiative series starts and stops.

    Monthly on purpose: a yearly version had to explain two kinds of
    incompleteness at once - a running year that has not finished, and
    initiatives that did not exist for all of their first year - and blind
    readers could not keep them apart. A month is a month, so the only rules
    left to protect are which months are in and which are out.
    """

    CORE = (["2022-11"] * 2 + ["2023-06"] * 4 + ["2024-01"] * 3
            + ["2025-05"] * 2 + ["2026-06"] * 5 + ["2026-07"] * 9)
    GROUPS = {
        "core": CORE,
        "canvas": ["2024-04", "2024-09", "2025-02", "2026-03"],
        "drupalCms": ["2024-03", "2025-11"],
        "ai": ["2024-06", "2026-01", "2024-08", "2026-02", "2026-02"],
    }

    def collect(self, today="2026-07-27", groups=None):
        groups = self.GROUPS if groups is None else groups
        return analyze_module.initiative_series(
            {group: collections.Counter(months) for group, months in groups.items()}, today[:7])

    def test_opens_a_full_year_before_the_first_initiative(self):
        # Not at core's first commit, which would draw two decades of initiatives
        # at zero, and not at the first initiative either, which leaves a reader
        # nothing to compare the arrival against. Earliest initiative is 2024-03.
        self.assertEqual(self.collect()[0]["date"], "2023-01")

    def test_the_running_month_is_left_out(self):
        # It is the only genuinely part-elapsed point on the chart, and drawn it
        # dips every series at once, which reads as a project-wide slowdown.
        months = [row["date"] for row in self.collect(today="2026-07-27")]
        self.assertNotIn("2026-07", months)
        self.assertEqual(months[-1], "2026-06")

    def test_a_finished_month_is_kept_even_when_it_is_the_latest(self):
        # Run in August: July has finished and must appear.
        months = [row["date"] for row in self.collect(today="2026-08-03")]
        self.assertEqual(months[-1], "2026-07")

    def test_months_are_contiguous_with_no_gaps(self):
        # A month nobody committed in must still be a point, otherwise the x axis
        # silently compresses quiet periods.
        months = [row["date"] for row in self.collect()]
        self.assertEqual(len(months), len(set(months)))
        self.assertEqual(months, sorted(months))
        self.assertEqual(len(months), 42)   # 2023-01 through 2026-06

    def test_every_group_appears_in_every_month(self):
        # A missing key reads as zero in the chart, which is indistinguishable
        # from a group that genuinely did nothing that month.
        for row in self.collect():
            for group in ("core", *STRATEGIC_REPOS):
                self.assertIn(group, row, f"{group} missing from {row['date']}")

    def test_months_before_a_repository_existed_are_zero_not_absent(self):
        # The chart turns these into nulls itself, so what is stored has to be a
        # real zero it can distinguish, not a missing key.
        first = self.collect()[0]
        self.assertEqual((first["date"], first["canvas"]), ("2023-01", 0))

    def test_no_initiative_history_yields_no_series(self):
        # Emitting core alone would publish a chart whose whole point is the comparison.
        self.assertEqual(self.collect(groups={"core": self.CORE}), [])

    def test_repositories_sum_into_their_group_counting_each_main_line(self):
        # An initiative repository that merges a branch without squashing counts the
        # merge once, the way core's commits count.
        with tempfile.TemporaryDirectory() as directory:
            core = Path(directory, "core")
            core.mkdir()
            git_history(core, [("main", "2024-03-05", "Issue #1 by alice: Core work")])
            repos = Path(directory, "ecosystem-repos")
            for name in (name for names in STRATEGIC_REPOS.values() for name in names):
                repository = Path(repos, f"{name}.git")
                repository.mkdir(parents=True)
                git_history(repository, [("main", "2024-03-10", f"Start {name}")])
            ai = Path(repos, "ai.git")
            run_git(ai, "checkout", "--quiet", "-b", "feature")
            commit_change(ai, "Feature work", "2024-03-20")
            commit_change(ai, "More feature work", "2024-03-21")
            run_git(ai, "checkout", "--quiet", "main")
            merge(ai, "feature", "Merge branch 'feature' into 'main'", "2024-03-22")
            rows = analyze_module.get_initiative_commits(analyze_module.landed_commits(core), repos, "2024-05")
        march = next(row for row in rows if row["date"] == "2024-03")
        # Each group counts one commit per repository it holds. The ai repository adds one
        # more: its branch merged without squashing, so the merge counts and the two commits
        # behind it do not. Derived from the list rather than written out, because the
        # subject here is the merge arithmetic, not how many repositories a band happens
        # to hold this month.
        self.assertEqual((march["core"], march["canvas"], march["drupalCms"]), (1, 1, 1))
        self.assertEqual(march["ai"], len(STRATEGIC_REPOS["ai"]) + 1)


class CreditsPerIssueTest(unittest.TestCase):
    """A person is credited once per issue, in the year of the first commit crediting
    them, on any branch, as drupal.org's credit system counts. The walk used to follow
    HEAD alone and missed every Drupal 7 and Drupal 6 maintenance credit, while counting
    a follow-up, backport or cherry-pick of the same issue as another credit."""

    def test_each_person_is_credited_once_per_issue_on_any_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2012-03-01", "Issue #10 by alice: Fix the thing"),
                ("main", "2013-03-01", "Issue #10 followup by alice, bob: Fix it more"),
                ("main", "2015-03-01", "SA-CORE-2015-001 by dave"),
                ("6.x", "2011-03-01", "Issue #30 by erin: A Drupal 6 only fix"),
                ("7.x", "2012-06-01", "Issue #10 by alice: Backport the fix"),
                ("7.x", "2014-03-01", "Issue #20 by carol: A Drupal 7 only fix"),
                ("7.x", "2015-03-01", "SA-CORE-2015-01 by dave"),
                ("8.0.x", "2013-04-01", "Issue #10 followup by alice, bob: Fix it more"),
            ])
            history = analyze_module.collect_credit_history(repository)
        self.assertEqual(history.names_by_year, {2011: {"erin"}, 2012: {"alice"}, 2013: {"bob"}, 2014: {"carol"}, 2015: {"dave"}})
        self.assertEqual(sum(sum(counts.values()) for counts in history.credit_counts_by_year.values()), 5)
        self.assertEqual(history.first_seen, {"alice": 2012, "bob": 2013, "carol": 2014, "dave": 2015, "erin": 2011})


class MainLineTest(unittest.TestCase):
    """A commit is one change landed on a repository's main line, and a snapshot is core
    as it stood on a date. A merged sandbox or feature branch counts once, as its merge:
    one AI initiative repository merges without squashing, a quarter of its commits in
    2025-2026. A pull merge is different. In 2011-2013 core committers pulled before
    pushing, git recorded the commits others had already pushed as the merge's second
    parent, and following first parents alone dropped 338 commits that had landed."""

    def landed_subjects(self, repository):
        return sorted(commit.subject for commit in analyze_module.landed_commits(repository))

    def test_a_merged_branch_counts_as_one_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2012-01-02", "Issue #1 by alice: Start"),
                ("sandbox", "2012-01-03", "Work in progress"),
                ("sandbox", "2012-01-04", "More work"),
            ])
            run_git(repository, "checkout", "--quiet", "main")
            merge(repository, "sandbox", "Merge the sandbox", "2012-01-05")
            months = analyze_module.get_commits_per_month(analyze_module.landed_commits(repository), "2012-02")
        self.assertEqual(sum(value for row in months for key, value in row.items() if key != "date"), 2)

    def test_the_commits_a_pull_merge_brought_in_landed(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2012-01-02", "Issue #1 by alice: Start"),
                ("pushed", "2012-01-03", "Issue #2 by bob: Pushed by another committer"),
                ("pushed", "2012-01-04", "Issue #3 by carol: Also pushed"),
                ("main", "2012-01-05", "Issue #4 by dave: Committed locally before pulling"),
            ])
            merge(repository, "pushed", "Merge branch '8.x' of git.drupal.org:project/drupal into 8.x", "2012-01-06")
            landed = analyze_module.landed_commits(repository)
            snapshot = analyze_module.snapshot_commit(landed, "2012-01-04")
        self.assertEqual(sorted(commit.subject for commit in landed), [
            "Issue #1 by alice: Start", "Issue #2 by bob: Pushed by another committer",
            "Issue #3 by carol: Also pushed", "Issue #4 by dave: Committed locally before pulling"])
        # Core as it stood on 2012-01-04 held the pushed commits, not the local one.
        self.assertEqual(next(commit.subject for commit in landed if commit.sha == snapshot), "Issue #3 by carol: Also pushed")

    def test_a_pull_into_the_default_branch_needs_no_into(self):
        # Newer git leaves out " into main" when pulling main into main.
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2024-01-02", "Start"),
                ("pushed", "2024-01-03", "Pushed by someone else"),
                ("main", "2024-01-04", "Committed locally"),
            ])
            merge(repository, "pushed", "Merge branch 'main' of https://git.example.com/project", "2024-01-05")
            self.assertEqual(self.landed_subjects(repository), ["Committed locally", "Pushed by someone else", "Start"])

    def test_pulling_a_different_branch_is_a_branch_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2024-01-02", "Start"),
                ("feature", "2024-01-03", "Feature work"),
                ("main", "2024-01-04", "Committed locally"),
            ])
            merge(repository, "feature", "Merge branch 'feature' of https://git.example.com/project", "2024-01-05")
            self.assertEqual(self.landed_subjects(repository),
                             ["Committed locally", "Merge branch 'feature' of https://git.example.com/project", "Start"])

    def test_a_snapshot_is_core_as_it_stood_on_its_date(self):
        # The newest commit before a date can sit on a branch merged later.
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2012-01-02", "Issue #1 by alice: Start"),
                ("sandbox", "2012-01-20", "Work in progress"),
                ("main", "2012-01-10", "Issue #2 by bob: Continue"),
            ])
            merge(repository, "sandbox", "Merge the sandbox", "2012-02-05")
            landed = analyze_module.landed_commits(repository)
            snapshot = analyze_module.snapshot_commit(landed, "2012-01-31")
        self.assertEqual(next(commit.subject for commit in landed if commit.sha == snapshot), "Issue #2 by bob: Continue")
        self.assertIsNone(analyze_module.snapshot_commit(landed, "2012-01-01"))


class FinishedMonthsTest(unittest.TestCase):
    """A monthly series holds every finished month: a month without commits is a zero,
    not missing, and the month the run happens in is left out. Commits per year took
    twelve months present as a complete year, so each December it drew the running
    year as finished."""

    def test_commits_per_month_are_every_finished_month(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = git_history(Path(directory), [
                ("main", "2012-01-10", "Issue #1 by alice: Start"),
                ("main", "2012-03-10", "Issue #2 by bob: Continue"),
                ("main", "2012-04-10", "Issue #3 by carol: This month"),
            ])
            months = analyze_module.get_commits_per_month(analyze_module.landed_commits(repository), "2012-04")
        self.assertEqual([(row.pop("date"), sum(row.values())) for row in months],
                         [("2012-01", 1), ("2012-02", 0), ("2012-03", 1)])


if __name__ == "__main__":
    unittest.main()
