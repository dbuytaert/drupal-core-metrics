"""The dashboard's chart rules in chart-rules.js, run in node on small series."""
import json
import subprocess
import unittest
from pathlib import Path

RULES = Path(__file__).parent.parent / "chart-rules.js"


def rule(name: str, *arguments):
    """The rule's result, 'NaN' for NaN (which JSON would turn into null), or
    {'error': message} when it throws."""
    call = ", ".join(json.dumps(argument) for argument in arguments)
    script = (f"let result; try {{ result = require({json.dumps(str(RULES))}).{name}({call}); }}"
              " catch (error) { result = { error: error.message }; }"
              " console.log(Number.isNaN(result) ? JSON.stringify('NaN') : JSON.stringify(result))")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def months(*shares, key="features"):
    """Months as (date, commits carrying a type, all commits)."""
    rows = []
    for date, typed, total in shares:
        row = {"date": date, "features": 0, "bugs": 0, "maintenance": 0, "uncategorized": total - typed}
        row[key] = typed
        rows.append(row)
    return rows


class MonthlyCommitsTest(unittest.TestCase):
    def test_every_type_counts(self):
        self.assertEqual(rule("commitTotal", {"features": 1, "bugs": 2, "maintenance": 4, "uncategorized": 8}), 15)


class ObjectOrientedSnapshotsTest(unittest.TestCase):
    """LCOM4 started at a written-in 2013-07. It starts where most production PHP is in
    classes, and stays started if the share later dips."""

    def test_starts_at_the_first_mostly_object_oriented_snapshot(self):
        snapshots = [{"date": "2012-07", "production": {"lines": 100, "objectOrientedLines": 50}},
                     {"date": "2013-07", "production": {"lines": 100, "objectOrientedLines": 53}},
                     {"date": "2014-01", "production": {"lines": 100, "objectOrientedLines": 49}}]
        self.assertEqual([snapshot["date"] for snapshot in rule("objectOrientedSnapshots", snapshots)],
                         ["2013-07", "2014-01"])


class DocumentedHookSnapshotsTest(unittest.TestCase):
    """The API surface series starts where Core began documenting hooks; before it the
    hooks band read as zero."""

    def test_starts_at_the_first_snapshot_with_a_documented_hook(self):
        snapshots = [{"date": "2008-07", "surfaceArea": {"hooks": []}},
                     {"date": "2009-01", "surfaceArea": {"hooks": ["hook_help"]}},
                     {"date": "2009-07", "surfaceArea": {"hooks": []}}]
        self.assertEqual([snapshot["date"] for snapshot in rule("documentedHookSnapshots", snapshots)],
                         ["2009-01", "2009-07"])
        self.assertEqual(rule("documentedHookSnapshots", [{"date": "2001-01"}]), [])


class ClassifiedMonthsTest(unittest.TestCase):
    """Commit types are drawn for up to twelve months, from two months after the last
    month in which most commits carried no type."""

    def test_starts_after_the_half_adopted_month(self):
        series = months(("2025-10", 0, 100), ("2025-11", 63, 100), ("2025-12", 100, 100), ("2026-01", 96, 100))
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)], ["2025-12", "2026-01"])

    def test_a_stray_typed_month_long_before_adoption_does_not_start_the_series(self):
        series = months(("2001-02", 1, 1), ("2025-10", 0, 100), ("2025-11", 63, 100), ("2025-12", 97, 100))
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)], ["2025-12"])

    def test_every_type_counts_as_classified(self):
        series = months(("2025-10", 0, 100), ("2025-11", 63, 100), ("2025-12", 97, 100), key="maintenance")
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)], ["2025-12"])

    def test_exactly_half_typed_is_not_yet_adopted(self):
        series = months(("2025-10", 50, 100), ("2025-11", 63, 100), ("2025-12", 97, 100))
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)], ["2025-12"])

    def test_a_month_without_commits_is_not_adoption(self):
        series = months(("2025-10", 0, 0), ("2025-11", 63, 100), ("2025-12", 97, 100))
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)], ["2025-12"])

    def test_keeps_the_last_twelve_months(self):
        series = months(*((f"2026-{number:02d}", 95, 100) for number in range(1, 13)), ("2027-01", 97, 100))
        self.assertEqual([month["date"] for month in rule("classifiedMonths", series)],
                         [f"2026-{number:02d}" for number in range(2, 13)] + ["2027-01"])


class QuarterlyCommitsTest(unittest.TestCase):
    """The initiatives chart draws whole quarters, each dated by its first month, from the
    monthly series data.json stores."""

    def test_whole_quarters_sum_their_months(self):
        months = [{"date": f"2024-{month:02d}", "core": month, "ai": 1} for month in range(1, 7)]
        self.assertEqual(rule("quarterlyCommits", months),
                         [{"date": "2024-01", "core": 6, "ai": 3}, {"date": "2024-04", "core": 15, "ai": 3}])

    def test_a_quarter_missing_a_month_is_left_out(self):
        # The months enter in February and end in July: neither outer quarter is whole.
        months = [{"date": f"2024-{month:02d}", "core": 1} for month in range(2, 8)]
        self.assertEqual(rule("quarterlyCommits", months), [{"date": "2024-04", "core": 3}])


class ReleasePositionTest(unittest.TestCase):
    """A release marker sits where the release happened on every card. Drupal 8 shipped
    in November 2015. Snapping to the next label drew it at 2016 on snapshot charts and at
    2015 on yearly ones; treating a year label as its middle then dropped a January
    release from the first yearly bar."""

    def test_on_dated_points_a_release_sits_between_the_points_around_it(self):
        self.assertAlmostEqual(rule("releasePosition", ["2015-01", "2015-07", "2016-01"], "2015-11"), 1 + 4 / 6)
        self.assertEqual(rule("releasePosition", ["2015-01", "2015-07"], "2015-07"), 1)
        self.assertEqual(rule("releasePosition", ["2015-01", "2015-07"], "2015-01"), 0)
        # Snapshots are six months apart except the latest.
        self.assertAlmostEqual(rule("releasePosition", ["2026-01", "2026-07", "2026-09"], "2026-08"), 1.5)
        self.assertIsNone(rule("releasePosition", ["2015-01", "2015-07"], "2014-12"))
        self.assertIsNone(rule("releasePosition", ["2015-01", "2015-07"], "2015-08"))

    def test_on_yearly_bars_a_release_sits_inside_its_year(self):
        bars = {"bars": True}
        self.assertAlmostEqual(rule("releasePosition", ["2011", "2012"], "2011-01", bars), -0.5)
        self.assertAlmostEqual(rule("releasePosition", ["2021", "2022"], "2022-12", bars), 1 + 5 / 12)
        self.assertIsNone(rule("releasePosition", ["2016", "2017"], "2015-11", bars))

    def test_on_a_day_axis_a_release_sits_on_the_first_day_of_its_month(self):
        # The page performance charts plot days since 1970; Drupal 11 shipped in August 2024.
        axis = {"dayAxis": {"min": 19875, "max": 20710}}  # 2024-06-01 to 2026-09-14
        self.assertEqual(rule("releasePosition", [], "2024-08", axis), 19936)  # 2024-08-01
        self.assertIsNone(rule("releasePosition", [], "2022-12", axis))
        self.assertIsNone(rule("releasePosition", [], "2026-12", axis))

    def test_a_chart_whose_labels_are_not_its_dates_passes_them(self):
        # Source code age labels its points by year, but each stands for a snapshot month.
        self.assertAlmostEqual(
            rule("releasePosition", ["2015", "2016"], "2015-11", {"pointDates": ["2015-07", "2016-07"]}), 4 / 12)
        self.assertIsNone(rule("releasePosition", ["2015", "2016"], "2015-11"))

    def test_an_axis_that_is_not_a_timeline_gets_no_marker(self):
        self.assertIsNone(rule("releasePosition", ["Jan", "Feb", "Mar"], "2015-11"))
        self.assertIsNone(rule("releasePosition", ["Jan", "Feb", "Mar"], "2015-11", {"bars": True}))

    def test_dates_out_of_order_fail_loudly(self):
        self.assertIn("error", rule("pointPosition", ["2016-07", "2016-01"], "2016-03"))
        self.assertIn("error", rule("yearBarPosition", ["2017", "2016"], "2016-03"))
        self.assertIn("error", rule("pointPosition", ["2016-07", None], "2016-03"))


class ColumnSharesTest(unittest.TestCase):
    """The Relative half of a stacked chart draws each series as its share of the column.
    Rounding each share to a display figure left columns summing to 99.9 and a hairline of
    background above the top band, so the rule divides and the tooltip rounds."""

    def test_each_column_sums_to_exactly_one_hundred(self):
        shares = rule("columnShares", [[1, 3], [3, 1]])
        self.assertEqual(shares, [[25, 75], [75, 25]])
        for column in range(2):
            self.assertEqual(sum(series[column] for series in shares), 100)

    def test_dividing_unrounded_beats_rounding_each_share(self):
        # A third of a column has no exact value in binary, so the sum lands a hair under
        # 100 however it is computed. What matters is the size of the gap: rounding each
        # share to the tooltip's one decimal leaves 99.9 and a visible line of background
        # above the top band, while dividing unrounded leaves less than a millionth of a
        # percent. That is why the rule divides and only the tooltip rounds.
        shares = rule("columnShares", [[1], [1], [1]])
        unrounded_gap = abs(100 - sum(series[0] for series in shares))
        rounded_gap = abs(100 - sum(round(series[0], 1) for series in shares))
        self.assertLess(unrounded_gap, 1e-9)
        self.assertAlmostEqual(rounded_gap, 0.1, places=9)

    def test_a_column_holding_nothing_is_zero_rather_than_a_division_by_zero(self):
        # An empty column is a real shape: every series starts at zero before the first
        # snapshot that has any of it.
        self.assertEqual(rule("columnShares", [[0, 2], [0, 2]]), [[0, 50], [0, 50]])

    def test_a_gap_in_a_series_counts_as_nothing(self):
        # The charts already read a missing value as nothing, so the share rule agrees.
        self.assertEqual(rule("columnShares", [[None, 1], [1, 1]]), [[0, 50], [100, 50]])

    def test_one_series_holds_the_whole_column(self):
        self.assertEqual(rule("columnShares", [[7, 9]]), [[100, 100]])

    def test_no_series_at_all_yields_nothing(self):
        self.assertEqual(rule("columnShares", []), [])


class YearStartsTest(unittest.TestCase):
    """A time axis ticks once per year, at the year's first label, so thinning a crowded
    axis can only drop whole years, never leave it blank."""

    def test_the_first_label_of_each_year(self):
        self.assertEqual(rule("yearStarts", ["2011-01", "2011-07", "2012-01", "2012-07", "2012-09"]), [0, 2])
        self.assertEqual(rule("yearStarts", ["2015", "2016", "2017"]), [0, 1, 2])
        self.assertEqual(rule("yearStarts", []), [])

    def test_a_year_the_axis_enters_partway_gets_no_tick(self):
        # The series opens in July 2000; its tick sat one snapshot before 2001's.
        self.assertEqual(rule("yearStarts", ["2000-07", "2001-01", "2001-07", "2002-01"]), [1, 3])


class LabelRowsTest(unittest.TestCase):
    """Marker labels stack by the space they take, not by rounded positions."""

    def test_a_label_takes_the_first_row_it_fits_in(self):
        boxes = [{"left": 0, "right": 50}, {"left": 30, "right": 80}, {"left": 60, "right": 110},
                 {"left": 55, "right": 90}, {"left": 120, "right": 170}]
        self.assertEqual(rule("labelRows", boxes), [0, 1, 0, 2, 0])

    def test_labels_closer_than_the_gap_do_not_share_a_row(self):
        self.assertEqual(rule("labelRows", [{"left": 0, "right": 50}, {"left": 52, "right": 90}], 4), [0, 1])


class RangeTest(unittest.TestCase):
    """A time range keeps a dated point when its date is inside, and a calendar year only
    when the whole year is, so yearly charts agree with commits per year."""

    def test_a_dated_point_is_in_range_when_its_month_is(self):
        self.assertTrue(rule("monthInRange", "2015-11", "2015-11", None))
        self.assertFalse(rule("monthInRange", "2015-07", "2015-11", None))
        self.assertFalse(rule("monthInRange", "2024-09", None, "2024-08"))
        self.assertTrue(rule("monthInRange", "2024-08", None, "2024-08"))

    def test_a_year_is_in_range_only_when_all_of_it_is(self):
        self.assertTrue(rule("yearInRange", 2011, "2011-01", None))
        self.assertFalse(rule("yearInRange", 2015, "2015-11", None))
        self.assertTrue(rule("yearInRange", 2016, "2015-11", None))
        self.assertFalse(rule("yearInRange", 2024, None, "2024-08"))
        self.assertTrue(rule("yearInRange", 2023, None, "2024-08"))


if __name__ == "__main__":
    unittest.main()
