#!/usr/bin/env python3
"""
Drupal Core Dashboard - Data Collection Script

Analyzes Drupal core across historical snapshots, assembling data.json. Metrics come
from two sources: the files of each snapshot (lines of code, complexity, cohesion,
anti-patterns, API surface, deprecations, hotspots), measured by extract-facts.php
(facts.py) and decided by the definitions in definitions.py; and git itself (commits,
credits, security advisories, source code age via blame). Every run starts from nothing:
facts are held in memory for the run, and no state carries over from an earlier one.
"""

import collections
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import definitions
import facts


# Configuration
DRUPAL_REPO_URL = "https://git.drupalcode.org/project/drupal.git"
DRUPAL_START_DATE = datetime(2000, 1, 1)  # Drupal's earliest history (CVS import, May 2000)


class Colors:
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    NC = "\033[0m"


def log_info(message: str):
    print(f"{Colors.GREEN}[INFO]{Colors.NC} {message}", flush=True)


def log_warn(message: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {message}", flush=True)


def log_error(message: str):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {message}", flush=True)


class GitError(RuntimeError):
    """A git command failed. History walks raise it instead of returning an empty series,
    so a failed walk stops the run rather than publishing a hole in a chart."""


GIT_TIMEOUT_SECONDS = 600


def git(repository: Path, *arguments: str, accepted_exit_codes: tuple[int, ...] = (0,)) -> str:
    """git's output for a command run in a repository; raises GitError when git fails or
    times out. accepted_exit_codes are the codes a command answers with rather than fails
    with, such as git grep's 1 for "no match"."""
    try:
        result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(arguments)} ran over {GIT_TIMEOUT_SECONDS} seconds in {repository}")
    if result.returncode not in accepted_exit_codes:
        raise GitError(f"git {' '.join(arguments)} failed in {repository}: {result.stderr.strip()}")
    return result.stdout


def sync_clone(url: str, target: Path, *clone_options: str) -> None:
    """Clone a repository bare, or bring an existing clone level with its remote: every
    branch and tag as the remote has them, and HEAD on the remote's default branch.
    Raises GitError when git fails.

    A bare clone has no fetch refspec, so a plain fetch moves nothing but FETCH_HEAD:
    the initiative counts froze in July 2026 while every fetch succeeded, and core's
    release branches never moved after the clone. Tags are fetched by force and pruned
    like branches: --tags refuses to move a tag the remote re-pointed, and failed every
    run after. HEAD is set once, at clone time, so a repository cloned while empty kept a
    HEAD with no commits, and one whose default branch changed kept counting the old
    branch; every sync points it at the remote's default branch.
    """
    if target.exists():
        git(target, "fetch", "origin", "--prune", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    else:
        git(target.parent, "clone", "--bare", "--quiet", *clone_options, url, str(target))
    remote_head = git(target, "ls-remote", "--symref", "origin", "HEAD")
    if remote_head.startswith("ref: "):
        git(target, "symbolic-ref", "HEAD", remote_head.split()[1])


def setup_drupal(drupal_dir: Path) -> bool:
    """Clone or update Drupal core. A failed update stops the run: carrying on with the
    old clone published stale data stamped with today's date."""
    log_info("Updating Drupal core..." if drupal_dir.exists() else "Cloning Drupal core...")
    try:
        sync_clone(DRUPAL_REPO_URL, drupal_dir)
    except GitError as error:
        log_error(f"Could not update Drupal core ({error}); nothing was written. Run again.")
        return False
    return True


# The message git writes when a committer pulls a branch into their own copy of it before
# pushing: "Merge branch '8.x' of git.drupal.org:project/drupal into 8.x". When the branch
# pulled into is the default branch, git leaves out " into <branch>".
PULL_MERGE = re.compile(r"^Merge branch '(?P<branch>[^']+)' of \S+(?: into (?P<into>\S+))?$")


class LandedCommit(NamedTuple):
    """A change landed on a repository's main line, dated by committer time."""
    sha: str
    timestamp: int
    subject: str

    @property
    def month(self) -> str:
        return utc_day(self.timestamp)[:7]


def utc_day(timestamp: int) -> str:
    """A Unix timestamp as a 'YYYY-MM-DD' day in UTC, the one clock every series uses."""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def main_line_ranges(repository: Path) -> tuple[list[list[str]], set[str]]:
    """The git log ranges that cover a repository's main line when walked with
    --first-parent, and the pull merges that join them.

    The main line follows first parents, so a merged sandbox or feature branch counts
    once, as its merge. A pull merge is the exception: both of its parents are the same
    branch, the second holding commits other committers had already pushed, so those
    commits landed too and the merge itself is bookkeeping. In 2011-2013 core committers
    pulled before pushing 57 times, and first parents alone dropped 338 landed commits.
    """
    default_branch = git(repository, "symbolic-ref", "--short", "HEAD").strip()
    ranges, pull_merges, pending = [], set(), [["HEAD"]]
    while pending:
        range_arguments = pending.pop()
        ranges.append(range_arguments)
        for line in git(repository, "log", "--first-parent", "--merges", "--format=%H%x1f%P%x1f%s",
                        *range_arguments).splitlines():
            sha, parents, subject = line.split("\x1f", 2)
            pull = PULL_MERGE.match(subject)
            if pull and (pull.group("into") or default_branch) == pull.group("branch"):
                first_parent, second_parent = parents.split()[:2]
                pull_merges.add(sha)
                pending.append([second_parent, f"^{first_parent}"])
    return ranges, pull_merges


def landed_commits(repository: Path, *paths: str) -> list[LandedCommit]:
    """Every change landed on a repository's main line (see main_line_ranges), newest
    first, limited to the changes under `paths` when given: the one definition of a commit
    for commits per month, the initiative series, snapshot selection and page performance.
    Committer time is when a change landed."""
    ranges, pull_merges = main_line_ranges(repository)
    landed = []
    for range_arguments in ranges:
        for line in git(repository, "log", "--first-parent", "--format=%H%x1f%ct%x1f%s", *range_arguments,
                        "--", *paths).splitlines():
            sha, timestamp, subject = line.split("\x1f", 2)
            if sha not in pull_merges:
                landed.append(LandedCommit(sha, int(timestamp), subject))
    return sorted(landed, key=lambda commit: commit.timestamp, reverse=True)


def snapshot_commit(landed: list[LandedCommit], target_date: str) -> Optional[str]:
    """The commit core stood at by the end of target_date in UTC: the latest change landed
    by then, or None before the first."""
    end_of_day = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86399
    candidates = [commit for commit in landed if commit.timestamp <= end_of_day]
    return max(candidates, key=lambda commit: commit.timestamp).sha if candidates else None


# Every published history: branches, and tags, which reach release commits no branch
# holds (four SA-CORE-2025-004 release commits). Remote-tracking refs a clone may still
# carry are left out.
ALL_HISTORY = ("--branches", "--tags")
ADVISORY = re.compile(r"SA-CORE-(\d{4})-(\d+)")


# Advisory identifiers are keyed on (year, number) as integers: early subjects
# zero-pad inconsistently, and SA-CORE-2009-03 is the same advisory as
# SA-CORE-2009-003. Keying on the raw string counted 2009 and 2010 twice.
def get_security_advisories(drupal_dir: Path, running_year: int) -> list[dict]:
    """SA-CORE advisories *named in a core commit subject*, per finished year.

    This is a floor, not the published total: a fix can land without naming its
    advisory, which loses 2 of 2013's 3 and 2 of 2014's 6. Three richer sources
    were tried and rejected. The drupal.org API (type=sa) covers only 2018
    onward and is incomplete even there, reporting 4 advisories for 2018 when 6
    exist. Taking the highest number in a year overcounts, because numbers are
    reserved and left unpublished: 2021 skips 12 and 13, 2022 skips 7. Probing
    drupal.org/sa-core-YYYY-NNN cannot be trusted on the status code either,
    since an unknown identifier is fuzzy-matched onto a neighbour and answers
    200 (sa-core-2021-025 serves SA-CORE-2021-005), so only parsing the
    canonical id back out of each page title distinguishes them, at 25 page
    fetches per year against someone else's server on a daily cron.

    So the count stays a floor and the chart says so, which is honest and needs
    no network. The dashboard subtitle carries the same caveat.
    """
    subjects = git(drupal_dir, "log", *ALL_HISTORY, "-E", "--grep=SA-CORE-[0-9]", "--pretty=%s")
    return advisories_by_year(subjects, running_year)


def advisories_by_year(subjects: str, running_year: int) -> list[dict]:
    """Distinct advisories per finished year, from commit subjects. The running year is
    left out: part-elapsed, it understates its own count and reads as a change in the
    security process. Every finished year from the first advisory on is stored, zeros
    included, as the monthly series store their empty months: a year left out draws no
    bar at all, which reads as missing data rather than as a quiet year."""
    advisories = {(int(year), int(number)) for year, number in ADVISORY.findall(subjects)}
    counts = collections.Counter(year for year, _ in advisories if year < running_year)
    if not counts:
        return []
    return [{"year": year, "count": counts[year]} for year in range(min(counts), running_year)]


# The metrics the Performance charts draw, as core's performance tests pin them for Umami's
# pages: under core's own scenario labels and metric names, so every number in data.json
# can be checked against the assertion file it came from. The labels survived every
# reshuffle of the test classes (#3612172 merged them, #3618432 moved the expectations into
# YAML files), where a class name did not: the umamiFrontAndRecipePages scenarios kept
# their labels through AssetAggregationAcrossPagesTest, the merged class and
# MultipleRequestsPerformanceTest. Only what a chart draws is listed.
PAGE_PERFORMANCE_METRICS = {
    "umamiNodePageColdCache": ("QueryCount", "CacheGetCount", "CacheSetCount", "ScriptBytes", "StylesheetBytes"),
    "umamiNodePageCoolCache": ("QueryCount", "CacheGetCount", "CacheSetCount"),
    "umamiFrontPageColdCache": ("QueryCount", "CacheGetCount", "CacheSetCount"),
    "administratorNodePage": ("QueryCount", "ScriptBytes", "StylesheetBytes"),
    "umamiFrontAndRecipePages": ("ScriptBytes", "StylesheetBytes"),
    "umamiFrontAndRecipePagesAuthenticated": ("ScriptBytes", "StylesheetBytes"),
}
PINNED_METRICS = sorted({metric for metrics in PAGE_PERFORMANCE_METRICS.values() for metric in metrics})
UMAMI_TESTS = "core/profiles/demo_umami/tests/"
PERFORMANCE_COLLECTION = "collectPerformanceData("
SCENARIO_CLOSING = re.compile(r"^\s*\}, (.+)\);")
LITERAL_LABEL = re.compile(r"'(\w+)'")
PINNED_METRIC_LINE = re.compile(
    r"'(?P<array_metric>{names})' => (?P<array_value>\d+)|^(?P<yaml_metric>{names}): (?P<yaml_value>\d+)"
    .format(names="|".join(PINNED_METRICS)))
# The issue a commit subject names: "Issue #3408713: ..." before November 2025,
# "perf: #3564937 ..." since.
ISSUE_NUMBER = re.compile(r"#(\d+)")


def read_pinned_metrics(drupal_dir: Path, commit: str) -> dict[str, dict[str, int]]:
    """The metrics each Umami performance scenario pins at one commit, as
    {label: {metric: value}}; raises GitError when git fails. A commit where Umami's
    tests no longer exist pins nothing."""
    # A broad fixed-string grep; the regexes above decide what matched, so the
    # patterns are written once. -I skips binary files, whose "Binary file matches"
    # lines have no content to split.
    patterns = [argument for marker in ("}, ", PERFORMANCE_COLLECTION, *PINNED_METRICS)
                for argument in ("-e", marker)]
    # git grep exits 1 for "no match", which is an answer, not a failure.
    stdout = git(drupal_dir, "grep", "-I", "-F", *patterns, commit, "--", UMAMI_TESTS, accepted_exit_codes=(0, 1))
    pinned = {}
    label_in_file = {}
    for line in stdout.splitlines():
        _, path, content = line.split(":", 2)
        # Before #3618432 a scenario's expectations follow the label that closes its
        # collection in the test class. A new collection clears the label, and so
        # does a closing argument that is not a literal, so an unnamed scenario's
        # numbers are never credited to the one before it.
        if PERFORMANCE_COLLECTION in content:
            label_in_file[path] = None
            continue
        closing = SCENARIO_CLOSING.match(content)
        if closing:
            label = LITERAL_LABEL.fullmatch(closing.group(1))
            label_in_file[path] = label.group(1) if label else None
            continue
        metric = PINNED_METRIC_LINE.search(content)
        if not metric:
            continue
        # Since #3618432 each scenario has its own <label>.yml.
        scenario = Path(path).stem if path.endswith(".yml") else label_in_file.get(path)
        if scenario:
            name = metric.group("array_metric") or metric.group("yaml_metric")
            pinned.setdefault(scenario, {})[name] = int(metric.group("array_value") or metric.group("yaml_value"))
    return pinned


def get_page_performance(drupal_dir: Path) -> list[dict]:
    """The metrics in PAGE_PERFORMANCE_METRICS, one row per commit that changed any of them:
    {date, commit, issue, metrics: {scenario: {metric: value}}}.

    Core's performance tests fail when a query or cache count moves by one, or a JavaScript
    or CSS total by more than 2 KB, so a change to the code lands together with a change to
    the pinned number: the history of the expectations is the history of the metrics, with
    no need to run anything. Byte totals are only as fine as that tolerance, and a size step
    can release drift that earlier commits built up. Dated by committer date, which follows
    the branch order, so steps never run backwards the way author dates can.

    `commit` is the commit that recorded the new values and `issue` the drupal.org issue its
    subject names, or None. That commit is where a change was recorded, which is usually but
    not always the change that caused it: a follow-up can record numbers an earlier commit
    left out of date.

    A metric is None whenever core does not pin it: before it starts, for a while if a commit
    drops a label that a later one (such as a revert) restores, and after it ends.

    Umami is hidden in preparation for leaving core (#3526560). A successor scenario would
    be a new line, never spliced onto an ended one.
    """
    # A pinned value changes only in a commit that updates Umami's tests. Oldest first.
    rows = []
    previous = None
    for commit in reversed(landed_commits(drupal_dir, UMAMI_TESTS)):
        pinned = read_pinned_metrics(drupal_dir, commit.sha)
        metrics = {scenario: {metric: pinned.get(scenario, {}).get(metric) for metric in names}
                   for scenario, names in PAGE_PERFORMANCE_METRICS.items()}
        nothing_pinned = all(value is None for values in metrics.values() for value in values.values())
        if metrics == previous or (not rows and nothing_pinned):
            continue
        previous = metrics
        issue = ISSUE_NUMBER.search(commit.subject)
        rows.append({"date": utc_day(commit.timestamp), "commit": commit.sha,
                     "issue": int(issue.group(1)) if issue else None, "metrics": metrics})

    for scenario, names in PAGE_PERFORMANCE_METRICS.items():
        for metric in names:
            values = [row["metrics"][scenario][metric] for row in rows]
            if values and values[-1] is None and any(value is not None for value in values):
                log_warn(f"{scenario} {metric} is no longer pinned; its line ends at its last pinned value")
    return rows


# Drupal's strategic work no longer all happens in core, so core's commit count
# alone stopped describing the project around 2024. These repositories are PINNED
# rather than discovered: searching the GitLab API for "ai_" returns 2,773
# projects and "canvas" returns 1,347, while missing `ai` and `drupal_cms`, the
# two that matter most. A pinned list rots visibly, in a reviewed commit, instead
# of silently changing a published number between two daily runs. Review it when
# an initiative launches or is renamed.
#
# Canvas and Drupal CMS are each one repository. The AI initiative is spread over
# many small ones - providers, vector-database back ends, and the modules built on
# top of them - so its band holds every repository that ships something Drupal
# installs (a module, a theme or a recipe) and has at least ten counted commits.
# Repositories holding only documentation, agent skills, marketing or coordination
# ship nothing Drupal runs and fall out by that rule rather than by name, as does
# one whose name ends in test, which is test code by the convention the codebase
# rules already read.
#
# Overlap was measured with landed_commits, the rule this file counts by, never
# `git rev-list --all`: the two disagree enormously, because `ai` holds 3,330
# commits across every ref and 1,315 on its main line. By the counting rule no
# repository here shares a commit with another, so none is counted twice - `tool`
# was split out of `ai` in mid-2024 and shares 95 commits with it on other refs,
# but none that either repository counts.
STRATEGIC_REPOS = {
    "canvas": ["canvas"],
    "drupalCms": ["drupal_cms"],
    "ai": ["ai", "ai_agents", "ai_agents_ossa", "ai_answers", "ai_audio_generator", "ai_bench",
           "ai_ckeditor", "ai_content_audit", "ai_content_review", "ai_content_suggestions",
           "ai_context", "ai_dashboard", "ai_decision_log", "ai_editor_actions", "ai_empathy",
           "ai_eval", "ai_grounding", "ai_index_health", "ai_integration_eca", "ai_logging",
           "ai_metering", "ai_migration", "ai_model_registry", "ai_observability_opentel_recipe",
           "ai_plus", "ai_policy_gateway", "ai_provenance", "ai_provider_amazeeio",
           "ai_provider_amazeeio_recipe", "ai_provider_azure", "ai_provider_cloudflare_gateway",
           "ai_provider_google_vertex", "ai_provider_huggingface", "ai_provider_openai",
           "ai_provider_universal", "ai_proving_ground", "ai_rag_search_chat", "ai_recipe_generator",
           "ai_related_content", "ai_search", "ai_search_block", "ai_seo",
           "ai_vdb_provider_pinecone", "ai_vdb_provider_postgres", "ai_vdb_provider_qdrant",
           "ai_vdb_provider_sqlite", "mcp_server", "tool"],
}
STRATEGIC_REPO_URL = "https://git.drupalcode.org/project/{name}.git"


def setup_strategic_repos(repos_dir: Path) -> bool:
    """Clone or update the pinned initiative repositories.

    Blobless (--filter=tree:0) because only `git log` runs against them, which
    reads commit objects alone; core stays a full clone because its source-age
    blame needs trees. All-or-nothing on purpose: a partial set silently lowers
    every initiative count, which is indistinguishable from work slowing down.
    """
    repos_dir.mkdir(exist_ok=True)
    for name in [name for names in STRATEGIC_REPOS.values() for name in names]:
        try:
            sync_clone(STRATEGIC_REPO_URL.format(name=name), repos_dir / f"{name}.git", "--filter=tree:0")
        except GitError as error:
            log_error(f"Could not obtain {name}: {error}")
            return False
    return True


def get_initiative_commits(core_landed: list[LandedCommit], repos_dir: Path, running_month: str) -> list[dict]:
    """Monthly commits in core and in each strategic initiative.

    Commits, not people: contributor identity is not comparable across these
    repositories, because core credits reviewers in its commit messages while the
    contributed modules mostly do not, so a person-based count would measure
    crediting convention rather than participation. Commits need no such
    reconciliation.

    A commit is what the commits chart counts (`landed_commits`): the main line only,
    so core's backports to release branches do not count twice, and an initiative that
    merges branches without squashing counts each merge once.
    """
    def by_month(landed: list[LandedCommit]) -> collections.Counter:
        return collections.Counter(commit.month for commit in landed)

    commits_by_group = {"core": by_month(core_landed)}
    for group, names in STRATEGIC_REPOS.items():
        commits_by_group[group] = sum((by_month(landed_commits(repos_dir / f"{name}.git")) for name in names),
                                      collections.Counter())
    return initiative_series(commits_by_group, running_month)


def initiative_series(commits_by_group: dict[str, collections.Counter], running_month: str) -> list[dict]:
    """The initiative rows: every group in every finished month, zeros included.

    Monthly rather than yearly, which is what makes the series honest without any
    caveat attached. A yearly version had to explain two different kinds of
    incompleteness at once - a running year that has not finished, and initiatives
    that did not exist for all of their first year - and readers could not keep
    them apart. A month is a month. The running month is dropped because it is the
    only genuinely partial point, and an initiative simply begins where it begins.

    Starts a full year before the first initiative appeared, so the series opens on
    twelve months of core by itself. The initiatives are then seen arriving against
    a baseline rather than at the very first tick, where a reader has nothing to
    compare the arrival with.
    """
    initiative_months = [min(counter) for group, counter in commits_by_group.items() if group != "core" and counter]
    if not initiative_months:
        return []
    start = f"{int(min(initiative_months)[:4]) - 1}-01"
    return [{"date": month, **{group: counter.get(month, 0) for group, counter in commits_by_group.items()}}
            for month in finished_months(start, running_month)]


# A --line-porcelain header: the commit, then the line's original and final numbers.
BLAME_HEADER = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")


def date_code_lines(drupal_dir: Path, commit: str, code_lines: dict[str, list[int]]) -> dict[str, int]:
    """How many of the given lines were last changed in each year, by git blame at
    commit. code_lines maps repository paths to the line numbers the definitions count as
    production code, so the years add up to exactly its lines of code.
    "Age" is the last change, so a reformatted old line reads as recent."""
    def years_of(path_and_lines: tuple[str, list[int]]) -> collections.Counter:
        path, numbers = path_and_lines
        blame = git(drupal_dir, "blame", "--line-porcelain", commit, "--", path)
        year_of_commit, year_of_line, current, final_line = {}, {}, None, 0
        for line in blame.split("\n"):
            header = BLAME_HEADER.match(line)
            if header:
                current, final_line = header.group(1), int(header.group(2))
            elif line.startswith("committer-time "):
                year_of_commit[current] = time.gmtime(int(line.split()[1])).tm_year
            elif line.startswith("\t"):
                year_of_line[final_line] = year_of_commit[current]
        return collections.Counter(year_of_line[number] for number in numbers if number in year_of_line)

    years = collections.Counter()
    # The source-code-age blame fan-out is the pipeline's only parallel work and most of a run's
    # time. One worker per core; `nice` keeps a local run from taking the machine over.
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        for per_file in executor.map(years_of, code_lines.items()):
            years.update(per_file)
    return {str(year): count for year, count in sorted(years.items())}


def classify_commit(subject: str) -> str:
    """Classify a commit by its message prefix.

    Returns the monthly-count key: 'bugs', 'features', 'maintenance', or 'uncategorized'.
    Security releases keep their "SA-CORE-" subjects rather than a prefix; they fix
    vulnerabilities, so they count as bugs instead of falling into uncategorized.
    """
    subject = subject.strip().lower()
    if subject.startswith(("fix:", "bug:", "sa-core-")):
        return "bugs"
    if subject.startswith("feat:"):
        return "features"
    if re.match(r"[a-z]+:", subject):
        return "maintenance"
    return "uncategorized"


# Contributor credits. Committers write the credit lists into commit messages
# (composed by hand in the early years, generated from the drupal.org credit
# system since its introduction around 2015), so commit messages are the
# project's credit record. Three conventions across the project's history:
#  - "Issue #N by alice, bob:" subject lines (the convention through 2025);
#  - "SA-CORE-YYYY-NNN by alice, bob" subject lines on security releases, which
#    credit researchers; some end there and some carry a colon and a summary, so
#    the list stops at the colon as the issue form does. Reading to the end of the
#    line instead swallowed the summary into the last name, losing that credit;
#  - "By: alice" body trailers (the commit format adopted in late 2025; 2025
#    itself carries a mix of both).
SUBJECT_CREDIT = re.compile(r"#\d+(?:\s+follow-?up)?\s+by ([^:]+):", re.IGNORECASE)
SECURITY_CREDIT = re.compile(r"SA-CORE-\d{4}-\d+ by ([^:]+)", re.IGNORECASE)
# A few trailers are indented or written "by:"; both still credit the person.
TRAILER_CREDIT = re.compile(r"^[ \t]*By:[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)

# The issue a credited commit belongs to: its issue number, or its security advisory.
CREDITED_ISSUE = re.compile(r"#(\d+)|SA-CORE-(\d{4})-(\d+)", re.IGNORECASE)

# Separators seen in credit lists: commas, pipes, and "alice and bob". The word
# boundaries keep names like "sandy" or "b_and_w" intact.
CREDIT_SEPARATORS = re.compile(r",|\||\band\b")

# The contributors series starts where commit credits become reliable: 96% or more of
# the commits landed on the main line carry them, every year from 2009. Walking every
# branch reads lower in 2011-2012 only because the Drupal 8 sandbox histories merged
# into those branches carry no credit lines. The same convention-start precedent as
# the SA-CORE advisory series (2009).
# Earlier history is still scanned so long-time contributors are not
# miscounted as first-timers.
CONTRIBUTORS_SINCE = 2010

# Ceremonial commits credit everyone who helped a whole initiative (the 2017
# D8-multilingual thank-you commit lists 1,661 people for a 3-line
# MAINTAINERS.txt change), duplicating credits already granted on the real
# issues. The largest genuine single-issue credit list in the history is 126,
# so anything above this limit is ignored.
CREDIT_LIST_LIMIT = 200


def extract_credits(subject: str, message: str) -> list[str]:
    """Credited contributor names in one commit, lowercased for normalization.

    Empty when the commit carries no credits (early CVS-era commits, the
    2011-2012 sandbox-merge workflow, release commits). Uncredited commits are
    simply not counted: their authors are the core committers, who are credited
    on other commits in the same period, so including them would only mix git
    author names into the credit-username namespace. Reverts re-credit their
    contributors only in the subject-credit era, where the revert quotes a
    subject that still carries the credit list (64 of 66 reverts in 2024). A
    trailer-era revert quotes a subject with no names and carries no "By:"
    trailers of its own, so it credits nobody (2 of 20 reverts in 2026). Both
    are accepted: the people did the work being reverted, and the difference
    moves credit counts, not the set of distinct people credited in a year.

    The "By:" trailer era sometimes writes usernames as @name; the @ is
    stripped so a veteran returning under the new format is not miscounted
    as a first-timer.
    """
    groups = (SUBJECT_CREDIT.findall(subject)
              + SECURITY_CREDIT.findall(subject)
              + TRAILER_CREDIT.findall(message))
    names = []
    for group in groups:
        for name in CREDIT_SEPARATORS.split(group):
            name = name.strip().lower().lstrip("@")
            # A name holding an issue reference is the parser having run past the credit list,
            # where a committer pasted the credit line into itself ("Issue #N by a, b, Issue #N
            # by a, b: summary"). A person's name never carries one: of the 10,130 names in the
            # full history none holds a '#', and all 8 that did were fragments of a subject,
            # "most of #drupal" among them.
            if name and "#" not in name:
                names.append(name)
    return names


# The core tier: the most-credited people of a year, and everyone tied with the last of
# them, so who is named never depends on the order the credit history was walked.
CORE_TIER_SIZE = 50

# People who have asked not to be listed by name on the dashboard. Their credits
# still count in every chart; only the two name lists leave them out.
NOT_LISTED_BY_NAME = {"ghost of drupal past"}


class CreditHistory(NamedTuple):
    """Everything one walk of the credit history yields.

    Read fields by name rather than unpacking, so adding a field never means
    changing every call site.
    """
    names_by_year: dict
    credit_counts_by_year: dict
    first_seen: dict


def collect_credit_history(drupal_dir: Path) -> CreditHistory:
    """The credit record of every published history, shared by every credit-based metric,
    counted the way drupal.org counts credit: a person once per issue, in the year of the
    first commit that credits them.

    Every branch and tag, because core maintained each major version on its own branch
    (6.x and 7.x until their end of life) and HEAD never reaches those commits. Once per
    issue, because one issue reaches several commits: a follow-up, a backport, a
    cherry-pick to a release branch, a revert. An issue is its number, else its security
    advisory (zero-padding varies, so the number is compared as an integer); a credited
    commit naming neither is not tied to an issue and is not counted, which no commit in
    core's history does. Dated by committer time, when the change landed.
    """
    stdout = git(drupal_dir, "log", *ALL_HISTORY, "--pretty=format:%x1e%ct%x1f%B")
    first_credit: dict[tuple, int] = {}
    for record in stdout.split("\x1e")[1:]:
        timestamp, message = record.split("\x1f", 1)
        subject = message.split("\n", 1)[0]
        names = extract_credits(subject, message)
        issue = CREDITED_ISSUE.search(subject)
        if not names or len(names) > CREDIT_LIST_LIMIT or not issue:
            continue
        key = issue.group(1) or (issue.group(2), int(issue.group(3)))
        year = int(utc_day(int(timestamp))[:4])
        for name in set(names):
            first_credit[key, name] = min(year, first_credit.get((key, name), year))

    names_by_year: dict[int, set] = {}
    credit_counts_by_year: dict[int, collections.Counter] = {}
    first_seen: dict[str, int] = {}
    for (_, name), year in first_credit.items():
        names_by_year.setdefault(year, set()).add(name)
        credit_counts_by_year.setdefault(year, collections.Counter())[name] += 1
        first_seen[name] = min(year, first_seen.get(name, year))
    return CreditHistory(names_by_year, credit_counts_by_year, first_seen)


def listed_people(names: list[str], first_seen: dict) -> dict:
    """The shape both dashboard name lists share: people in alphabetical order with
    the year each was first credited, leaving out NOT_LISTED_BY_NAME and counting
    how many were left out."""
    listed = sorted((name for name in names if name not in NOT_LISTED_BY_NAME), key=str.lower)
    return {"people": [{"name": name, "firstCredited": first_seen[name]} for name in listed],
            "unlisted": len(names) - len(listed)}


def get_most_credited(credit_history: CreditHistory, running_year: int) -> dict:
    """The latest complete year's most-credited people, by name: the CORE_TIER_SIZE highest
    credit counts, and everyone tied with the last of them.

    Returns {year, people, unlisted}, the shape of listed_people plus the year. Each person
    carries the year they were first credited, so the dashboard bands them into generations
    without a regeneration. Credit counts are not stored, since no chart draws them, and the
    names are alphabetical rather than ranked: a credit count is not a ranking of people.

    Ranked across the whole year rather than within each generation: the newest generation is
    flat, so half of its credits is half of its people, and that version of the table named
    most of them.
    """
    year = running_year - 1
    credits = credit_history.credit_counts_by_year.get(year, collections.Counter())
    counts = sorted(credits.values(), reverse=True)
    cutoff = counts[CORE_TIER_SIZE - 1] if len(counts) >= CORE_TIER_SIZE else 0
    names = [name for name, count in credits.items() if count >= cutoff]
    return {"year": year, **listed_people(names, credit_history.first_seen)}


def get_half_of_credits_people(credit_history: CreditHistory, running_year: int) -> dict:
    """The people behind the latest complete year's halfOfCredits count, by name:
    the most-credited people until their credits reach half the total. People tied
    on credits at the cutoff fall in the order the credit history was walked, so a
    tie there names one of them; the number of names always matches the count.

    Returns {year, people, unlisted}, the shape of listed_people plus the year.
    NOT_LISTED_BY_NAME is left out of the names but not out of the group, so the
    charted count never changes.
    """
    year = running_year - 1
    credits = credit_history.credit_counts_by_year.get(year, collections.Counter())
    names = [name for name, _ in credits.most_common(half_of_credits_size(credits))]
    return {"year": year, **listed_people(names, credit_history.first_seen)}


def half_of_credits_size(credits: collections.Counter) -> int:
    """How few people account for half of these credits: the most-credited
    people, counted until their credits reach half the total.

    Credit-list width biases this the conservative way: a person's count is capped by
    commits, so wider lists dilute the top of the distribution rather than inflating it."""
    counts = sorted(credits.values(), reverse=True)
    total, running = sum(counts), 0
    for size, count in enumerate(counts, 1):
        running += count
        if running * 2 >= total:
            return size
    return 0


def get_contributors_per_year(credit_history: CreditHistory, running_year: int) -> list[dict]:
    """Distinct credited contributors per year, from CONTRIBUTORS_SINCE onward.

    Returns [{year, peopleCredited, firstTimeCredited, halfOfCredits, creditsByFirstYear}]:
    how many distinct people were credited; how many of them were credited for
    the first time ever; how concentrated the year's credits were; and
    creditsByFirstYear: the year's credits grouped by the year each credited
    person was first credited. Every field feeds a chart - the model carries
    nothing that is not drawn.

    creditsByFirstYear mirrors sourceCodeAge's lastTouched: storage per exact
    year, banding into generations left to the chart, so rebanding never needs a
    regeneration.

    Credits only: uncredited commits are not counted (measured effect: under 3%
    in the worst year, since their authors are committers credited elsewhere in
    the same period). Only complete years are emitted - a partial year reads as
    a collapse. First credits from before 2010 are real but can lag a veteran's actual start,
    which is why the dashboard groups them into one "before 2010" band.
    """
    names_by_year = credit_history.names_by_year
    credit_counts_by_year = credit_history.credit_counts_by_year
    first_seen = credit_history.first_seen

    series = []
    for year in sorted(names_by_year):
        if year < CONTRIBUTORS_SINCE or year >= running_year:
            continue
        first_timers = {name for name in names_by_year[year] if first_seen[name] == year}

        # Every credit of the year, grouped by when the credited person was first
        # credited, so each generation's share of the work needs no top-N cutoff.
        credits_by_first_year = collections.Counter()
        for name, count in credit_counts_by_year[year].items():
            credits_by_first_year[first_seen[name]] += count

        series.append({
            "year": year,
            "peopleCredited": len(names_by_year[year]),
            "firstTimeCredited": len(first_timers),
            "halfOfCredits": half_of_credits_size(credit_counts_by_year[year]),
            "creditsByFirstYear": {str(first_year): count for first_year, count in sorted(credits_by_first_year.items())},
        })
    return series


def finished_months(first: str, running_month: str) -> list[str]:
    """Every month from `first` up to, not including, running_month (the month the run
    happens in), as 'YYYY-MM': the months of every monthly series. A month without
    commits is a zero rather than missing, and the running month is left out, because
    drawn it reads as a drop. Charts that roll months into years count on both: commits
    per year took any year with twelve months present as complete, so each December it
    drew the running year as finished. The running month is passed in, not read from the
    clock, so the whole run agrees on it and a test can set it."""
    months = []
    year, month = int(first[:4]), int(first[5:7])
    while f"{year:04d}-{month:02d}" < running_month:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def get_commits_per_month(landed: list[LandedCommit], running_month: str) -> list[dict]:
    """Commits landed per finished month, classified by type: [{date, features, bugs,
    maintenance, uncategorized}] oldest first. The monthly total is derivable (sum of
    the type counts), so it is not stored."""
    if not landed:
        return []
    month_counts = {month: {"features": 0, "bugs": 0, "maintenance": 0, "uncategorized": 0}
                    for month in finished_months(min(commit.month for commit in landed), running_month)}
    for commit in landed:
        if commit.month in month_counts:
            month_counts[commit.month][classify_commit(commit.subject)] += 1
    return [{"date": month, **counts} for month, counts in month_counts.items()]


def plan_snapshots(landed: list[LandedCommit], head: str, now: datetime) -> list[tuple[str, str, bool]]:
    """The snapshots to analyze, as (month, commit, dated): the main line at the start of every
    January and July since Drupal began, then HEAD for the running month unless the last
    snapshot already falls in it. Source code age dates one snapshot a year: the last of each
    past year, and for this year HEAD, or the last snapshot when it falls in HEAD's month."""
    running_month = now.strftime("%Y-%m")
    months = [month for year in range(DRUPAL_START_DATE.year, now.year + 1)
              for month in (f"{year}-01", f"{year}-07") if month <= running_month]
    last_of_year = {month[:4]: month for month in months}
    planned = []
    for month in months:
        commit = snapshot_commit(landed, f"{month}-01")
        if commit is None:
            log_warn(f"No commit found for {month}")
            continue
        dated = last_of_year[month[:4]] == month and (int(month[:4]) < now.year or month == running_month)
        planned.append((month, commit, dated))
    if not planned or planned[-1][0] != running_month:
        planned.append((running_month, head, True))
    return planned


def analyze_snapshots(drupal_dir: Path, planned: list[tuple[str, str, bool]]) -> tuple[list[dict], list[dict]]:
    """The codebase fields of each planned snapshot, given as (month, commit, dated), and the
    source code age of each dated one: its production lines by the year git blame dates them,
    which must add up to its lines of code."""
    commits = [commit for _, commit, _ in planned]
    log_info(f"Measuring the files of {len(set(commits))} snapshots")
    trees = facts.measure(drupal_dir, commits)
    snapshots, source_code_age = [], []
    for month, commit, dated in planned:
        snapshot = definitions.Snapshot(trees[commit])
        # A handful of files in 2001-2003 are not valid PHP in any version; the count is surfaced
        # rather than hidden, since such a file counts lines but declares nothing.
        if snapshot.unparsed_production_php:
            log_warn(f"{len(snapshot.unparsed_production_php)} production files failed to parse for {month}")
        fields = snapshot.fields()
        snapshots.append({"date": month, **fields})
        if dated:
            last_touched = date_code_lines(drupal_dir, commit, snapshot.production_code_lines())
            lines = fields["production"]["lines"]
            if sum(last_touched.values()) != lines:
                raise RuntimeError(f"git blame dated {sum(last_touched.values())} of {lines} production lines at {month}")
            # `date` is the snapshot the year's point stands for: July for past years, the
            # latest commit for the current one, so the chart places release markers by it.
            source_code_age.append({"year": int(month[:4]), "date": month, "lastTouched": last_touched})
    return snapshots, source_code_age


def to_index_ranges(indices: list[int]) -> list[list[int]]:
    """Collapse a sorted index list into contiguous [start, end] ranges."""
    ranges: list[list[int]] = []
    for index in sorted(indices):
        if ranges and index == ranges[-1][1] + 1:
            ranges[-1][1] = index
        else:
            ranges.append([index, index])
    return ranges


def separate_surface_area(snapshots: list[dict]) -> tuple[list[dict], dict]:
    """The snapshots without their surfaceArea lists, and those lists inverted into
    {category: {item: [[start, end], ...]}} by snapshot index. The API surface is ~97%
    identical between adjacent snapshots, so recording each item once with the index
    ranges where it exists is ~10x smaller than a full list per snapshot; index.html
    reconstructs the per-snapshot lists on load.
    """
    presence: dict = {}
    for index, snapshot in enumerate(snapshots):
        for category, items in snapshot["surfaceArea"].items():
            by_item = presence.setdefault(category, {})
            for item in items:
                by_item.setdefault(item, []).append(index)
    inverted = {category: {item: to_index_ranges(indices) for item, indices in by_item.items()}
                for category, by_item in presence.items()}
    return [{key: value for key, value in snapshot.items() if key != "surfaceArea"} for snapshot in snapshots], inverted


# Every one of these series is append-only: it covers completed months, years,
# snapshots, or change points, so a regeneration can add entries but never
# legitimately lose them.
APPEND_ONLY_SERIES = ("monthlyCommits", "contributors", "initiatives", "pagePerformance",
                      "securityAdvisories", "snapshots", "sourceCodeAge")


def refuse_degraded_write(data: dict, data_file: Path, allow_shrink: bool) -> None:
    """Abort before overwriting data.json with a degraded regeneration.

    A failed git command raises and stops the run, but a collector can still come back
    empty or short without failing: a source that moved, a pattern that stopped
    matching. The run would then report success and write a data.json whose charts are
    simply gone. The daily workflow commits that, and since there is no per-chart error
    isolation, by design, the live dashboard renders the loss silently.

    So the run fails loudly instead, on either an empty series or one that lost
    entries against the committed file. Pass --allow-shrink for the deliberate
    case, such as narrowing a series' start year, where a shrink is the intent.
    """
    empty = [name for name in (*APPEND_ONLY_SERIES, "mostCredited", "halfOfCreditsPeople", "surfaceArea")
             if not data.get(name)]
    if empty:
        log_error(f"Refusing to write {data_file.name}: empty series {', '.join(empty)}. "
                  "A collector found nothing; data.json is unchanged.")
        sys.exit(1)

    if not data_file.exists():
        return
    with open(data_file) as handle:
        previous = json.load(handle)

    shrunk = [(name, len(previous[name]), len(data[name]))
              for name in APPEND_ONLY_SERIES
              if isinstance(previous.get(name), list) and len(data[name]) < len(previous[name])]
    if shrunk:
        detail = ", ".join(f"{name} {before} -> {after}" for name, before, after in shrunk)
        if allow_shrink:
            log_warn(f"Series shrank ({detail}), writing anyway because --allow-shrink was given.")
        else:
            log_error(f"Refusing to write {data_file.name}: {detail}. These series only grow, "
                      "so this points at a failed or partial collection. Re-run, or pass "
                      "--allow-shrink if the shrink is intended.")
            sys.exit(1)


def main():
    # Setup paths
    project_dir = Path(__file__).parent.parent.resolve()
    drupal_dir = project_dir / "drupal-core"
    data_file = project_dir / "data.json"

    if sys.argv[1:] not in ([], ["--allow-shrink"]):
        log_error("Usage: analyze.py [--allow-shrink]")
        sys.exit(2)
    allow_shrink = sys.argv[1:] == ["--allow-shrink"]

    log_info("Starting Drupal Core metrics collection")
    repos_dir = project_dir / "ecosystem-repos"

    # Setup Drupal
    if not setup_drupal(drupal_dir):
        sys.exit(1)

    # UTC, like every other clock read in this file: a local-time "now" near a month boundary
    # disagrees with the UTC one used for the initiative series and generatedAt. One clock for
    # the whole run: the running month and year every series leaves out.
    now = datetime.now(timezone.utc)
    running_month = now.strftime("%Y-%m")
    landed = landed_commits(drupal_dir)
    planned = plan_snapshots(landed, git(drupal_dir, "rev-parse", "HEAD").strip(), now)
    snapshots, sourceCodeAge = analyze_snapshots(drupal_dir, planned)
    log_info(f"Dated source code age across {len(sourceCodeAge)} years")

    # Commits per month (the dashboard rolls these up to years; no yearly series stored).
    monthlyCommits = get_commits_per_month(landed, running_month)
    log_info(f"Counted commits across {len(monthlyCommits)} months")

    credit_history = collect_credit_history(drupal_dir)
    contributors = get_contributors_per_year(credit_history, now.year)
    mostCredited = get_most_credited(credit_history, now.year)
    halfOfCreditsPeople = get_half_of_credits_people(credit_history, now.year)
    log_info(f"Counted contributors across {len(contributors)} years; "
             f"{len(mostCredited['people'])} people named across the generations; "
             f"{len(halfOfCreditsPeople['people'])} people with half the credits")

    if not setup_strategic_repos(repos_dir):
        log_error("Could not obtain every pinned initiative repository. Counting a "
                  "partial set would lower each initiative's commits for reasons "
                  "unrelated to the work, so this run stops.")
        sys.exit(1)
    initiatives = get_initiative_commits(landed, repos_dir, running_month)
    log_info(f"Counted initiative commits across {len(initiatives)} months"
             + (f", {initiatives[0]['date']} to {initiatives[-1]['date']}"
                if initiatives else ""))

    securityAdvisories = get_security_advisories(drupal_dir, now.year)
    log_info(f"Found security advisories across {len(securityAdvisories)} years")

    pagePerformance = get_page_performance(drupal_dir)
    log_info(f"Found {len(pagePerformance)} rows of pinned page performance metrics")

    snapshots, surfaceArea = separate_surface_area(snapshots)

    # Build final data structure.
    data = {
        "generatedAt": now.isoformat(),
        "monthlyCommits": monthlyCommits,
        "contributors": contributors,
        "mostCredited": mostCredited,
        "halfOfCreditsPeople": halfOfCreditsPeople,
        "securityAdvisories": securityAdvisories,
        "initiatives": initiatives,
        "pagePerformance": pagePerformance,
        "sourceCodeAge": sourceCodeAge,
        "snapshots": snapshots,
        "surfaceArea": surfaceArea,
    }

    refuse_degraded_write(data, data_file, allow_shrink)

    # Save results as JSON
    with open(data_file, "w") as handle:
        json.dump(data, handle, indent=2)

    log_info(f"Analysis complete! Processed {len(snapshots)} snapshots.")
    log_info(f"Data saved to: {data_file}")


if __name__ == "__main__":
    main()
