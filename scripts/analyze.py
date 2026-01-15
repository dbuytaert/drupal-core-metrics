#!/usr/bin/env python3
"""
Drupal Core Dashboard - Data Collection Script

Analyzes Drupal core across historical snapshots, collecting metrics like
LOC, CCN, MI, anti-patterns, and API surface area. Uses drupalisms.php for all analysis.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# Configuration
DRUPAL_REPO_URL = "https://git.drupalcode.org/project/drupal.git"
DRUPAL_START_DATE = datetime(2011, 1, 1)  # Start from Drupal 7 release


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


def log_debug(message: str):
    """Print debug message if DEBUG environment variable is set."""
    if os.environ.get("DEBUG"):
        print(f"[DEBUG] {message}", flush=True)


def run_command(cmd: list[str], cwd: Optional[str] = None, capture: bool = True) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            timeout=600  # 10 minute timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def setup_drupal(drupal_dir: Path) -> bool:
    """Clone or update Drupal core repository."""
    if drupal_dir.exists():
        log_info("Drupal core already exists, fetching updates...")
        # Fetch remote HEAD and update local HEAD's target ref
        code, _, err = run_command(["git", "fetch", "origin", "--tags"], cwd=str(drupal_dir))
        if code != 0:
            log_error(f"Failed to fetch: {err}")
            return False
        code, head_ref, _ = run_command(["git", "symbolic-ref", "HEAD"], cwd=str(drupal_dir))
        if code == 0:
            run_command(["git", "update-ref", head_ref.strip(), "FETCH_HEAD"], cwd=str(drupal_dir))
    else:
        log_info("Cloning Drupal core...")
        code, _, err = run_command(["git", "clone", "--bare", DRUPAL_REPO_URL, str(drupal_dir)])
        if code != 0:
            log_error(f"Failed to clone: {err}")
            return False
    return True


def get_commit_for_date(drupal_dir: Path, target_date: str) -> Optional[str]:
    """Get the commit hash closest to the target date."""
    code, stdout, _ = run_command(
        ["git", "rev-list", "-1", f"--before={target_date}T23:59:59", "HEAD"],
        cwd=str(drupal_dir)
    )
    if code == 0 and stdout.strip():
        return stdout.strip()
    return None


def get_commits_per_year(drupal_dir: Path) -> list[dict]:
    """Count commits per year from git history.

    Returns list of {year, commits} sorted by year ascending.
    """
    # Get all commit dates (just the year)
    code, stdout, _ = run_command(
        ["git", "log", "--pretty=format:%ad", "--date=format:%Y"],
        cwd=str(drupal_dir)
    )
    if code != 0 or not stdout.strip():
        return []

    # Count commits per year
    year_counts = {}
    for line in stdout.strip().split('\n'):
        year = line.strip()
        if year:
            year_counts[year] = year_counts.get(year, 0) + 1

    # Convert to sorted list
    result = [{"year": int(year), "commits": count} for year, count in year_counts.items()]
    result.sort(key=lambda x: x["year"])
    return result


def classify_commit(subject: str) -> str:
    """Classify a commit by its message using Conventional Commits specification.

    Format: <type>[optional scope][!]: <description>
    See: https://www.conventionalcommits.org/en/v1.0.0/

    Returns: 'Bug', 'Feature', 'Maintenance', or 'Unknown'
    """
    subject = subject.strip().lower()

    # Conventional commits pattern: type(optional-scope)!: description
    # Types that indicate bugs
    bug_pattern = r'^(fix|bugfix|bug|hotfix)(\([^)]+\))?!?:'
    if re.match(bug_pattern, subject):
        return "Bug"

    # Types that indicate features
    feature_pattern = r'^(feat|feature)(\([^)]+\))?!?:'
    if re.match(feature_pattern, subject):
        return "Feature"

    # Types that indicate maintenance/tasks
    maintenance_pattern = r'^(build|chore|ci|docs|style|refactor|perf|test|task|revert)(\([^)]+\))?!?:'
    if re.match(maintenance_pattern, subject):
        return "Maintenance"

    # Drupal.org issue references - check content after issue number
    # "Issue #123... Add something" -> check for feature/bug keywords
    issue_match = re.match(r'^issue\s*#?\d+[^:]*:\s*(.+)', subject)
    if issue_match:
        # Extract the description after "Issue #XXX by authors:"
        description = issue_match.group(1).strip()
        # Recursively classify the description
        return classify_commit(description)

    # Merge commits
    if subject.startswith("merge"):
        return "Maintenance"

    # Keyword-based fallback for non-conventional commits
    # Feature keywords (from ws_feature_analyzer patterns)
    feature_keywords = [
        'add ', 'added ', 'adding ', 'adds ',
        'new ', 'implement', 'introduce', 'create',
        'support for', 'ability to', 'now supports', 'now allows',
        'enhancement', 'enhanced', 'improved', 'improvement'
    ]
    if any(kw in subject for kw in feature_keywords):
        return "Feature"

    # Bug keywords
    bug_keywords = [
        'fix ', 'fixed ', 'fixes ', 'fixing ',
        'bug ', 'error', 'crash', 'broken', 'wrong', 'resolve'
    ]
    if any(kw in subject for kw in bug_keywords):
        return "Bug"

    # Maintenance keywords
    maintenance_keywords = [
        'refactor', 'cleanup', 'clean up', 'update ', 'updates ', 'upgrade',
        'docs', 'documentation', 'test', 'style', 'deprecat', 'revert',
        'remove ', 'removed ', 'delete', 'rename', 'move '
    ]
    if any(kw in subject for kw in maintenance_keywords):
        return "Maintenance"

    return "Unknown"


def get_commits_per_month(drupal_dir: Path) -> list[dict]:
    """Count commits per month from git history, classified by type.

    Returns list of {date, total, features, bugs, maintenance, unknown} sorted by date ascending.
    """
    code, stdout, _ = run_command(
        ["git", "log", "--pretty=format:%ad|%s", "--date=format:%Y-%m"],
        cwd=str(drupal_dir)
    )
    if code != 0 or not stdout.strip():
        return []

    month_counts = {}
    for line in stdout.strip().split('\n'):
        if '|' not in line:
            continue
        date, subject = line.split('|', 1)
        date = date.strip()

        if date not in month_counts:
            month_counts[date] = {"total": 0, "features": 0, "bugs": 0, "maintenance": 0, "unknown": 0}

        month_counts[date]["total"] += 1
        commit_type = classify_commit(subject)
        if commit_type == "Bug":
            month_counts[date]["bugs"] += 1
        elif commit_type == "Feature":
            month_counts[date]["features"] += 1
        elif commit_type == "Maintenance":
            month_counts[date]["maintenance"] += 1
        else:
            month_counts[date]["unknown"] += 1

    result = [{"date": date, **counts} for date, counts in month_counts.items()]
    result.sort(key=lambda x: x["date"])
    return result


def export_version(drupal_dir: Path, commit: str, work_dir: Path) -> bool:
    """Export a specific version of Drupal to work directory."""
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    # Use git archive piped directly to tar (binary mode to handle non-text files)
    try:
        git_proc = subprocess.Popen(
            ["git", "archive", commit],
            cwd=str(drupal_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        tar_proc = subprocess.Popen(
            ["tar", "-x", "-C", str(work_dir)],
            stdin=git_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        git_proc.stdout.close()
        tar_proc.communicate(timeout=300)
        git_proc.wait()
        return tar_proc.returncode == 0 and git_proc.returncode == 0
    except Exception as e:
        log_warn(f"Failed to archive {commit[:8]}: {e}")
        return False


def get_recent_commits(drupal_dir: Path, days: int = 365) -> list[dict]:
    """Get recent commits.

    Returns list of {hash, message, date, lines, type} sorted by date descending.
    """
    code, stdout, _ = run_command(
        ["git", "log", f"--since={days} days ago", "--pretty=format:COMMIT:%H:%cs:%s", "--shortstat"],
        cwd=str(drupal_dir)
    )
    if code != 0:
        return []

    commits = []
    current_hash = None
    current_msg = None
    current_date = None

    for line in stdout.split('\n'):
        line = line.strip()
        if line.startswith('COMMIT:'):
            parts = line.split(':', 3)
            if len(parts) >= 4:
                current_hash = parts[1]
                current_date = parts[2]
                current_msg = parts[3][:80]
        elif 'changed' in line and current_hash:
            insertions = deletions = 0
            match_ins = re.search(r'(\d+) insertion', line)
            match_del = re.search(r'(\d+) deletion', line)
            if match_ins:
                insertions = int(match_ins.group(1))
            if match_del:
                deletions = int(match_del.group(1))
            total = insertions + deletions
            # Convert YYYY-MM-DD to "Mon DD, YYYY" format for display
            try:
                dt = datetime.strptime(current_date, "%Y-%m-%d")
                formatted_date = dt.strftime("%b %d, %Y")
            except ValueError:
                formatted_date = current_date
            commits.append({
                'hash': current_hash,
                'message': current_msg,
                'date': formatted_date,
                'sort_date': current_date,
                'lines': total,
                'type': classify_commit(current_msg)
            })
            current_hash = None

    commits = sorted(commits, key=lambda x: x['sort_date'], reverse=True)
    for c in commits:
        del c['sort_date']
    return commits


def get_changed_files(drupal_dir: Path, commit_hash: str) -> list[str]:
    """Get list of PHP files changed in a commit."""
    code, stdout, _ = run_command(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash],
        cwd=str(drupal_dir)
    )
    if code != 0:
        return []

    php_extensions = {'.php', '.module', '.inc', '.install', '.theme', '.profile', '.engine'}
    files = []
    for line in stdout.strip().split('\n'):
        if line and any(line.endswith(ext) for ext in php_extensions):
            files.append(line)
    return files


def export_changed_files(drupal_dir: Path, commit_hash: str, files: list[str],
                         output_dir: Path) -> bool:
    """Export only specific files from a commit.

    Exports files individually to handle new/deleted files gracefully.
    Files that don't exist in this commit are silently skipped.
    """
    if not files:
        return True

    output_dir.mkdir(parents=True, exist_ok=True)
    exported_count = 0

    for file_path in files:
        # Check if file exists in this commit
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit_hash}:{file_path}"],
            cwd=str(drupal_dir),
            capture_output=True
        )
        if result.returncode != 0:
            # File doesn't exist in this commit (new or deleted)
            continue

        # Get file content
        result = subprocess.run(
            ["git", "show", f"{commit_hash}:{file_path}"],
            cwd=str(drupal_dir),
            capture_output=True
        )
        if result.returncode != 0:
            continue

        # Write to output directory
        output_file = output_dir / file_path
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_bytes(result.stdout)
        exported_count += 1

    return exported_count > 0


def analyze_commit_delta(drupal_dir: Path, commit_hash: str, work_dir: Path) -> Optional[dict]:
    """Analyze metric deltas for a single commit.

    Only analyzes files changed in the commit for speed.
    Returns deltas for LOC, CCN, MI, and anti-patterns.
    """
    # Get parent commit
    code, stdout, _ = run_command(
        ["git", "rev-parse", f"{commit_hash}^"],
        cwd=str(drupal_dir)
    )
    if code != 0 or not stdout.strip():
        return None
    parent_hash = stdout.strip()

    # Get list of changed PHP files
    changed_files = get_changed_files(drupal_dir, commit_hash)
    if not changed_files:
        return {"locDelta": 0, "ccnDelta": 0, "miDelta": 0, "antipatternsDelta": 0}

    scripts_dir = Path(__file__).parent
    php_script = scripts_dir / "drupalisms.php"
    if not php_script.exists():
        return {"locDelta": 0, "ccnDelta": 0, "miDelta": 0, "antipatternsDelta": 0}

    work_dir.mkdir(parents=True, exist_ok=True)

    def get_metrics(directory: Path) -> dict:
        """Get metrics for files in directory.

        Returns totals (not averages) for meaningful deltas:
        - ccnSum: sum of CCN across all functions (higher = more complexity)
        - miDebtSum: sum of (100 - MI) across all functions (higher = more debt)
        """
        if not directory.exists() or not any(directory.rglob("*.php")):
            return {"loc": 0, "ccnSum": 0, "miDebtSum": 0, "antipatterns": 0}
        try:
            result = subprocess.run(
                ["php", "-d", "memory_limit=512M", str(php_script), str(directory)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                prod = data.get("production", {})
                loc = prod.get("loc", 0)
                # Convert antipatterns density back to absolute count
                antipatterns = int(prod.get("antipatterns", 0) * loc / 1000) if loc > 0 else 0
                return {
                    "loc": loc,
                    "ccnSum": data.get("ccnSum", 0),
                    "miDebtSum": data.get("miDebtSum", 0),
                    "antipatterns": antipatterns
                }
        except Exception:
            pass
        return {"loc": 0, "ccnSum": 0, "miDebtSum": 0, "antipatterns": 0}

    # Export and analyze parent
    parent_dir = work_dir / "parent"
    if parent_dir.exists():
        shutil.rmtree(parent_dir)
    export_changed_files(drupal_dir, parent_hash, changed_files, parent_dir)
    parent_metrics = get_metrics(parent_dir)

    # Export and analyze commit
    commit_dir = work_dir / "commit"
    if commit_dir.exists():
        shutil.rmtree(commit_dir)
    export_changed_files(drupal_dir, commit_hash, changed_files, commit_dir)
    commit_metrics = get_metrics(commit_dir)

    # Calculate deltas using totals (always meaningful, even for new/deleted files)
    # Note: miDelta is inverted (parent - commit) so positive = improved maintainability
    return {
        "locDelta": commit_metrics["loc"] - parent_metrics["loc"],
        "ccnDelta": commit_metrics["ccnSum"] - parent_metrics["ccnSum"],
        "miDelta": parent_metrics["miDebtSum"] - commit_metrics["miDebtSum"],
        "antipatternsDelta": commit_metrics["antipatterns"] - parent_metrics["antipatterns"],
    }


def analyze_recent_commits(drupal_dir: Path, output_dir: Path,
                           target_count: int = 100) -> list[dict]:
    """Analyze commits until we find target_count with metric changes.

    Only includes commits where CCN, MI, or anti-patterns changed.
    Returns list of commits with their metric deltas.
    """
    commits = get_recent_commits(drupal_dir, days=365)
    if not commits:
        return []

    log_info(f"Scanning commits for {target_count} with metric changes...")
    work_dir = output_dir / "commit_work"
    results = []

    def has_metric_changes(delta: dict) -> bool:
        return any(delta[key] != 0 for key in ['ccnDelta', 'miDelta', 'antipatternsDelta'])

    for commit in commits:
        if len(results) >= target_count:
            break

        delta = analyze_commit_delta(drupal_dir, commit['hash'], work_dir)
        if delta and has_metric_changes(delta):
            log_info(f"Commit {commit['hash'][:11]} has metric changes ({len(results) + 1}/{target_count})")
            results.append({
                "hash": commit['hash'][:11],
                "date": commit['date'],
                "type": commit['type'],
                "message": commit['message'],
                **delta,
            })

    if work_dir.exists():
        shutil.rmtree(work_dir)

    log_info(f"Found {len(results)} commits with metric changes")
    return results


def analyze_directory(directory: Path, php_script: Path) -> Optional[dict]:
    """Analyze a directory using drupalisms.php."""
    php_files = list(directory.rglob("*.php")) + list(directory.rglob("*.module"))
    if not php_files:
        log_debug(f"No PHP files found in {directory}")
        return None

    log_debug(f"Found {len(php_files)} PHP files to analyze in {directory.name}")

    try:
        result = subprocess.run(
            ["php", "-d", "memory_limit=2G", str(php_script), str(directory)],
            capture_output=True,
            text=True,
            timeout=600
        )
        if result.returncode != 0:
            log_debug(f"PHP analysis failed: {result.stderr[:500] if result.stderr else 'no error output'}")
            return None

        if not result.stdout.strip():
            log_debug("PHP analysis returned empty output")
            return None

        data = json.loads(result.stdout)
        log_debug(f"PHP analysis returned data with keys: {list(data.keys())}")
        return data
    except json.JSONDecodeError as e:
        log_debug(f"JSON decode error: {e}")
        return None
    except Exception as e:
        log_debug(f"Exception during analysis: {e}")
        return None


def analyze_version(drupal_dir: Path, commit: str, year_month: str,
                    output_dir: Path, current: int = 0, total: int = 0,
                    collect_per_module: bool = False) -> Optional[dict]:
    """Analyze a single version of Drupal using drupalisms.php.

    Returns a snapshot dict with production, test, surfaceArea, and antipatterns.
    If collect_per_module is True, also includes per-module breakdown.
    """
    work_dir = output_dir / "work"

    progress = f" [{current}/{total}]" if total else ""
    log_info(f"Analyzing {year_month} (commit: {commit[:8]}){progress}")

    if not export_version(drupal_dir, commit, work_dir):
        return None

    # Only analyze D8+ with core/ directory
    core_dir = work_dir / "core"
    if not core_dir.is_dir():
        log_warn(f"No core/ directory for {year_month}, skipping")
        return None

    scripts_dir = Path(__file__).parent
    php_script = scripts_dir / "drupalisms.php"

    data = analyze_directory(core_dir, php_script)
    if not data:
        log_warn(f"Analysis returned no data for {year_month}")
        return None

    result = {
        "date": year_month,
        "commit": commit[:8],
        "production": data.get("production", {}),
        "testLoc": data.get("testLoc", 0),
        "surfaceArea": data.get("surfaceArea", {}),
        "surfaceAreaLists": data.get("surfaceAreaLists", {}),
        "antipatterns": data.get("antipatterns", {}),
        "hotspots": data.get("hotspots", []),
    }

    # Collect per-module stats for current snapshot
    if collect_per_module:
        modules_dir = core_dir / "modules"
        if modules_dir.is_dir():
            per_module = []
            for module_path in sorted(modules_dir.iterdir()):
                if module_path.is_dir() and not module_path.name.startswith('.'):
                    module_data = analyze_directory(module_path, php_script)
                    if module_data:
                        per_module.append({
                            "name": module_path.name,
                            "loc": module_data.get("production", {}).get("loc", 0),
                            "ccn": module_data.get("production", {}).get("ccn", {}).get("avg", 0),
                            "mi": module_data.get("production", {}).get("mi", {}).get("avg", 0),
                            "antipatterns": sum(module_data.get("antipatterns", {}).values()),
                        })
            # Sort by LOC descending
            per_module.sort(key=lambda x: x["loc"], reverse=True)
            result["perModule"] = per_module
            log_info(f"Collected stats for {len(per_module)} core modules")

    return result


def main():
    # Setup paths
    project_dir = Path(__file__).parent.parent.resolve()
    drupal_dir = project_dir / "drupal-core"
    output_dir = project_dir / "output"
    data_file = project_dir / "data.json"

    log_info("Starting Drupal Core metrics collection")

    # Create output directory
    output_dir.mkdir(exist_ok=True)

    # Setup Drupal
    if not setup_drupal(drupal_dir):
        sys.exit(1)

    # Build list of semi-annual snapshots to analyze (every 6 months)
    today = datetime.now()
    target = DRUPAL_START_DATE.replace(day=1, month=1)  # Start at January
    snapshot_dates = []
    while target <= today:
        snapshot_dates.append(target)
        new_month = target.month + 6
        if new_month > 12:
            target = target.replace(year=target.year + 1, month=new_month - 12)
        else:
            target = target.replace(month=new_month)

    total = len(snapshot_dates)
    log_info(f"Analyzing {total} semi-annual snapshots")

    snapshots = []
    for i, target in enumerate(snapshot_dates, 1):
        target_date = target.strftime("%Y-%m-%d")
        year_month = target.strftime("%Y-%m")

        commit = get_commit_for_date(drupal_dir, target_date)
        if commit:
            result = analyze_version(drupal_dir, commit, year_month, output_dir, i, total)
            if result:
                snapshots.append(result)
        else:
            log_warn(f"No commit found for {year_month}")

    # Always analyze current HEAD to ensure charts are up-to-date
    # Also collect per-module stats for the current version
    log_info("Analyzing current HEAD with per-module breakdown...")
    code, head_commit, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(drupal_dir))
    per_module_data = []
    if code == 0 and head_commit.strip():
        current_date = datetime.now().strftime("%Y-%m")
        # Always collect per-module data from HEAD
        result = analyze_version(drupal_dir, head_commit.strip(), current_date, output_dir,
                                 collect_per_module=True)
        if result:
            per_module_data = result.pop("perModule", [])
            # Only add snapshot if not already covered by the last one
            if not snapshots or snapshots[-1]["date"] != current_date:
                snapshots.append(result)

    # Cleanup work directory
    work_dir = output_dir / "work"
    if work_dir.exists():
        shutil.rmtree(work_dir)

    # Analyze recent commits for per-commit deltas
    commits = analyze_recent_commits(drupal_dir, output_dir)
    log_info(f"Analyzed {len(commits)} recent commits")

    # Get commit counts per year and month
    commitsPerYear = get_commits_per_year(drupal_dir)
    log_info(f"Counted commits across {len(commitsPerYear)} years")

    commitsMonthly = get_commits_per_month(drupal_dir)
    log_info(f"Counted commits across {len(commitsMonthly)} months")

    # Keep hotspots and surfaceAreaLists in each snapshot for historical dropdowns

    # Build final data structure
    data = {
        "generated": datetime.now().isoformat(),
        "commitsMonthly": commitsMonthly,
        "snapshots": snapshots,
        "commits": commits,
        "commitsPerYear": commitsPerYear,
        "perModule": per_module_data,
    }

    # Save results as JSON with error handling
    try:
        json_str = json.dumps(data, indent=2)
        if not json_str or json_str == "{}":
            log_error("Generated JSON is empty!")
            sys.exit(1)
        with open(data_file, "w") as f:
            f.write(json_str)
        log_debug(f"Wrote {len(json_str)} bytes to {data_file}")
    except (IOError, OSError) as e:
        log_error(f"Failed to write data file: {e}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        log_error(f"JSON encoding error: {e}")
        sys.exit(1)

    log_info(f"Analysis complete! Processed {len(snapshots)} snapshots.")
    log_info(f"Data saved to: {data_file}")


if __name__ == "__main__":
    main()
