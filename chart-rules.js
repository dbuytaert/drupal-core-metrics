// Pure rules the dashboard's charts follow: where a series starts, which points a time
// range keeps, where a date sits on an axis. Loaded by index.html and run by
// tests/test_chart_rules.py, so the rule a chart draws is the rule that is tested.

const MONTH = /^(\d{4})-(\d{2})$/;
const YEAR = /^\d{4}$/;

// Months since year zero for a 'YYYY-MM' date. Anything else throws, so a chart handing
// over something other than dates fails loudly instead of drawing in the wrong place.
const monthNumber = date => {
    const match = MONTH.exec(date);
    if (!match) throw new Error(`Not a YYYY-MM date: ${date}`);
    return Number(match[1]) * 12 + Number(match[2]) - 1;
};

const increasing = values => values.every((value, index) => index === 0 || values[index - 1] < value);

const startAtFirst = (entries, predicate) => {
    const start = entries.findIndex(predicate);
    return start < 0 ? [] : entries.slice(start);
};

const chartRules = {
    // Total commits in a month, derived from the type breakdown (not stored).
    commitTotal: month => month.features + month.bugs + month.maintenance + month.uncategorized,

    // LCOM4 is an average over classes, so its series starts at the first snapshot
    // where most production PHP sits in classes. Before that the average covered a few
    // dozen classes, and a trend measured from there said LCOM4 had risen when it has
    // fallen since 2009. Snapshots oldest first.
    objectOrientedSnapshots: snapshots => startAtFirst(snapshots, snapshot => snapshot.production?.objectOrientedLines > snapshot.production?.lines / 2),

    // The API surface series starts at the first snapshot with a documented hook. Hooks
    // are counted from Core's own documentation of them, which began in November 2008;
    // before it the hooks band read as zero, as if Drupal had no hooks. Oldest first.
    documentedHookSnapshots: snapshots => startAtFirst(snapshots, snapshot => snapshot.surfaceArea?.hooks?.length > 0),

    // How a value moved, as the word a subtitle uses instead of a figure: 'rose' or
    // 'fell' when it moved by more than `tolerance`, a fraction of where it started;
    // otherwise 'held'.
    movement: (before, after, tolerance) =>
        after > before * (1 + tolerance) ? 'rose' : after < before * (1 - tolerance) ? 'fell' : 'held',

    // The last twelve months whose commits classify by type. Core adopted
    // conventional-commit subjects ("fix:", "feat:") in November 2025, and months before
    // read as uncategorized, which a blind reader took for "a data outage". The series
    // starts two months after the last month in which most commits carry no type: the
    // month right after it is the half-adopted switch, and a stray typed commit in some
    // early month cannot start the series years too soon. Months oldest first, finished
    // and without gaps.
    classifiedMonths: monthlyCommits => {
        const lastUntyped = monthlyCommits.findLastIndex(month => month.uncategorized * 2 >= chartRules.commitTotal(month));
        return monthlyCommits.slice(lastUntyped < 0 ? 0 : lastUntyped + 2).slice(-12);
    },

    // Commits per whole quarter, each dated by its first month, from monthly rows of
    // { date, group: count, ... }. A quarter still missing a month, the running one or one
    // the months enter or leave partway, is left out rather than drawn short.
    quarterlyCommits: months => {
        const quarters = new Map();
        for (const month of months) {
            const first = monthNumber(month.date) - monthNumber(month.date) % 3;
            const date = `${Math.floor(first / 12)}-${String(first % 12 + 1).padStart(2, '0')}`;
            const quarter = quarters.get(date) || { date, months: 0 };
            quarter.months += 1;
            for (const [group, count] of Object.entries(month)) {
                if (group !== 'date') quarter[group] = (quarter[group] || 0) + count;
            }
            quarters.set(date, quarter);
        }
        return [...quarters.values()].filter(quarter => quarter.months === 3).map(({ months, ...quarter }) => quarter);
    },

    // Where a date falls on an axis of dated points, as a fractional index, or null
    // outside the first and last point. Points are placed by month, so unevenly spaced
    // snapshots interpolate correctly.
    pointPosition: (pointDates, date) => {
        const points = pointDates.map(monthNumber);
        if (!increasing(points)) throw new Error('Point dates must increase');
        const target = monthNumber(date);
        if (!points.length || target < points[0] || target > points[points.length - 1]) return null;
        const after = points.findIndex(point => point >= target);
        return points[after] === target
            ? after
            : after - 1 + (target - points[after - 1]) / (points[after] - points[after - 1]);
    },

    // Where a date falls on an axis of yearly bars, or null when no bar covers its year.
    // A bar spans its whole year, from index - 0.5 in January to index + 0.5 after
    // December; placing years at their middle dropped a January release from the first bar.
    yearBarPosition: (years, date) => {
        if (!years.every(year => YEAR.test(year)) || !increasing(years.map(Number))) {
            throw new Error('Bar years must be YYYY and increase');
        }
        const month = monthNumber(date);
        const index = years.indexOf(String(Math.floor(month / 12)));
        return index < 0 ? null : index - 0.5 + (month % 12) / 12;
    },

    // Where a release date sits on a chart's x axis, or null when the axis is not a
    // timeline. A chart whose labels are not its points' dates passes pointDates;
    // 'YYYY-MM' labels are dated points; 'YYYY' labels are yearly spans on a bar chart.
    // Year labels on a line chart are not guessed at, and month names get no markers. A
    // chart on a day axis (days since 1970) passes the axis's { min, max } as dayAxis, and
    // a release sits on the first day of its month.
    releasePosition: (labels, date, { bars = false, pointDates = null, dayAxis = null } = {}) => {
        if (dayAxis) {
            const month = monthNumber(date);
            const day = Date.UTC(Math.floor(month / 12), month % 12, 1) / 86400000;
            return day < dayAxis.min || day > dayAxis.max ? null : day;
        }
        if (pointDates) return chartRules.pointPosition(pointDates, date);
        if (labels.length && labels.every(label => MONTH.test(label))) return chartRules.pointPosition(labels, date);
        if (bars && labels.length && labels.every(label => YEAR.test(label))) return chartRules.yearBarPosition(labels, date);
        return null;
    },

    // The label indexes that start a new year: the ticks of a time axis. Chart.js thins a
    // crowded axis after formatting every label, so blanking all but the first label of
    // each year let it keep mostly blank ticks and Lines of code showed only "2000".
    // Building the axis from these ticks alone leaves it only years to thin. A year the
    // axis enters partway gets no tick: the series opens in July 2000, one snapshot
    // before 2001's tick, and the two labels overlapped.
    yearStarts: labels => labels.reduce((starts, label, index) => {
        const text = String(label);
        const startsYear = index === 0
            ? YEAR.test(text) || text.endsWith('-01')
            : text.slice(0, 4) !== String(labels[index - 1]).slice(0, 4);
        return startsYear ? [...starts, index] : starts;
    }, []),

    // The row each marker label goes in, taking labels left to right: the first row whose
    // last label ends at least `gap` pixels before this one starts. Rounding positions to
    // a label index put neighboring releases on one row, and their labels overlapped.
    labelRows: (boxes, gap = 4) => {
        const rowEnds = [];
        return boxes.map(({ left, right }) => {
            let row = rowEnds.findIndex(end => end + gap <= left);
            if (row < 0) row = rowEnds.length;
            rowEnds[row] = right;
            return row;
        });
    },

    // Each series as its share of what all of them hold at that point, which is what the
    // Relative half of a stacked chart draws. Unrounded, so a column sums to exactly 100
    // and the bands meet the top with no seam; the tooltip rounds for display. A column
    // holding nothing is all zeros rather than a division by zero, and a gap in a series
    // counts as nothing, as the charts already read a missing value.
    columnShares: series => {
        const totals = (series[0] || []).map((_, index) => series.reduce((sum, data) => sum + (data[index] || 0), 0));
        return series.map(data => data.map((value, index) => totals[index] ? (value || 0) / totals[index] * 100 : 0));
    },

    // Whether a dated point ('YYYY-MM') lies in a range whose bounds are 'YYYY-MM' or null.
    monthInRange: (date, start, end) => (!start || date >= start) && (!end || date <= end),

    // Whether a calendar year lies wholly in a range. A year the range cuts through is left
    // out, as commits per year leaves out a year with months missing, so every yearly
    // chart starts and ends a range at the same year.
    yearInRange: (year, start, end) => (!start || `${year}-01` >= start) && (!end || `${year}-12` <= end),
};

if (typeof module !== 'undefined') module.exports = chartRules;
