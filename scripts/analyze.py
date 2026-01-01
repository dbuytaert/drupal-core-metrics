#!/usr/bin/env python3
"""
Drupal Core Dashboard - Data Collection Script

Analyzes Drupal core across historical snapshots, collecting metrics like
LOC, CCN, MI, anti-patterns, and API surface area. Uses drupalisms.php for all analysis.
"""

import json
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
    """Classify a commit by its message prefix.

    Returns: 'Bug', 'Feature', 'Maintenance', or 'Unknown'
    """
    subject = subject.strip().lower()
    if subject.startswith(("fix:", "bug:")):
        return "Bug"
    elif subject.startswith("feat:"):
        return "Feature"
    elif subject.startswith(("task:", "docs:", "ci:", "test:", "perf:", "chore:", "refactor:")):
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
    """Export only specific files from a commit."""
    if not files:
        return True

    output_dir.mkdir(parents=True, exist_ok=True)

    # Use git archive with specific paths
    result = subprocess.run(
        ["git", "archive", "--format=tar", commit_hash, "--"] + files,
        cwd=str(drupal_dir),
        capture_output=True
    )
    if result.returncode != 0:
        return False

    # Extract the archive
    subprocess.run(
        ["tar", "-xf", "-"],
        input=result.stdout,
        cwd=str(output_dir),
        capture_output=True
    )
    return True


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
        """Get aggregate metrics for files in directory."""
        if not directory.exists() or not any(directory.rglob("*.php")):
            return {"loc": 0, "ccn": 0, "mi": 0, "antipatterns": 0}
        try:
            result = subprocess.run(
                ["php", "-d", "memory_limit=512M", str(php_script), str(directory)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                # Sum LOC, CCN, antipatterns across all files; average MI
                files = [f for f in data.get("files", []) if not f.get("test")]
                total_loc = sum(f.get("loc", 0) for f in files)
                total_ccn = sum(f.get("ccn", 0) for f in files)
                total_antipatterns = sum(f.get("antipatterns", 0) for f in files)
                avg_mi = sum(f.get("mi", 0) * f.get("loc", 0) for f in files) / max(total_loc, 1)
                return {"loc": total_loc, "ccn": total_ccn, "mi": round(avg_mi), "antipatterns": total_antipatterns}
        except Exception:
            pass
        return {"loc": 0, "ccn": 0, "mi": 0, "antipatterns": 0}

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

    return {
        "locDelta": commit_metrics["loc"] - parent_metrics["loc"],
        "ccnDelta": commit_metrics["ccn"] - parent_metrics["ccn"],
        "miDelta": commit_metrics["mi"] - parent_metrics["mi"],
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


def generate_html(project_dir: Path, data: dict) -> None:
    """Generate index.html with embedded data.

    Args:
        data: Dict with 'generated', 'snapshots', and 'commits' keys
    """
    html_file = project_dir / "index.html"

    data_json = json.dumps(data, indent=2)

    # Use current date for "last updated" (when the dashboard was generated)
    latest_date = datetime.now().strftime("%B %d, %Y")

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Drupal Core Metrics - Code Quality Dashboard</title>
    <meta name="description" content="Track Drupal core's code quality over time. See lines of code, cyclomatic complexity, maintainability index, anti-patterns, and API surface area.">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <style>
        :root {{
            --color-good: #22c55e;
            --color-warning: #f59e0b;
            --color-bad: #ef4444;
            --bg-primary: #ffffff;
            --bg-secondary: #f8fafc;
            --text-primary: #1e293b;
            --text-secondary: #64748b;
            --border-color: #e2e8f0;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: var(--bg-secondary);
            color: var(--text-primary);
            line-height: 1.6;
            padding: clamp(1rem, 4vw, 2rem);
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        header {{ margin-bottom: 2rem; }}
        h1 {{ font-size: 1.75rem; font-weight: 600; margin-bottom: 0.25rem; }}
        h2 {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 0.25rem; }}
        .header-meta {{ font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 1.5rem; text-transform: uppercase; letter-spacing: 0.05em; }}
        .executive-summary {{ margin: 0 0 0.75rem 0; line-height: 1.7; color: var(--text-secondary); }}
        .card {{
            background: var(--bg-primary);
            border-radius: 0.75rem;
            padding: clamp(1rem, 3vw, 1.5rem);
            margin-bottom: 1.5rem;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}
        .section-subtitle {{ font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.75rem; }}
        .metric-list {{ font-size: 0.8125rem; color: var(--text-secondary); margin: 0 0 1rem 0; padding-left: 1.25rem; line-height: 1.6; }}
        .metric-list li {{ margin-bottom: 0.125rem; }}
        .metric-list strong {{ color: var(--text-primary); font-weight: 500; }}
        code {{ font-family: 'SF Mono', Monaco, 'Courier New', monospace; font-size: 0.9em; background: var(--bg-secondary); padding: 0.1em 0.3em; border-radius: 0.25rem; }}
        .chart-container {{ position: relative; height: 300px; }}
        .hotspots-table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
        .hotspots-table th {{ text-align: left; padding: 0.75rem; border-bottom: 2px solid var(--border-color); font-weight: 600; color: var(--text-secondary); }}
        .hotspots-table td {{ padding: 0.75rem; border-bottom: 1px solid var(--border-color); }}
        .hotspots-table tr:hover {{ background: var(--bg-secondary); }}
        .hotspots-table .class-name {{ font-family: 'SF Mono', Monaco, 'Courier New', monospace; font-size: 0.8125rem; }}
        .metric-bad {{ color: var(--color-bad); font-weight: 600; }}
        .metric-warning {{ color: var(--color-warning); font-weight: 600; }}
        .metric-good {{ color: var(--color-good); }}
        button {{ cursor: pointer; border: 1px solid var(--border-color); background: var(--bg-primary); }}
        button:hover {{ background: var(--bg-secondary); }}
        button.active {{ background: var(--text-primary); color: var(--bg-primary); border-color: var(--text-primary); }}
        .toggle-button {{ margin-top: 1rem; padding: 0.5rem 1rem; border-radius: 0.375rem; }}
        .sort-button {{ padding: 0.25rem 0.5rem; margin-left: 0.25rem; border-radius: 0.25rem; font-size: 0.75rem; }}
        .surface-area-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
        .sa-panel {{ border: 1px solid var(--border-color); border-radius: 0.5rem; overflow: hidden; }}
        .sa-panel-header {{ padding: 0.75rem 1rem; background: var(--bg-secondary); font-weight: 500; font-size: 0.875rem; }}
        .sa-panel .sa-list {{ padding: 0.75rem 1rem; max-height: 200px; overflow-y: auto; font-size: 0.8125rem; font-family: 'SF Mono', Monaco, 'Courier New', monospace; }}
        .sa-panel .sa-list div {{ padding: 0.125rem 0; color: var(--text-secondary); }}
        .error {{ text-align: center; padding: 2rem; color: var(--color-bad); background: #fef2f2; border-radius: 0.5rem; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Drupal Core Metrics</h1>
            <p class="header-meta">Last updated: {latest_date}</p>
            <p class="executive-summary">The charts below tell a story of trade-offs.</p>
            <p class="executive-summary">Code quality has improved since Drupal 7: lower complexity, fewer anti-patterns, better architecture, and better test coverage. At the same time, the API surface area has grown: more plugin types, more hooks, more YAML formats, more ways to extend the system.</p>
            <p class="executive-summary">A larger surface area isn't necessarily bad. Targeted APIs can make common tasks easier. But it may correlate with a steeper learning curve for newcomers. By tracking these metrics, we hope to inform decisions about both code quality and developer experience.</p>
        </header>
        <div id="dashboard"></div>
    </div>
    <script>
        const DATA = {data_json};

        const colors = {{ production: '#3b82f6', test: '#8b5cf6' }};

        // Plugin to draw threshold bands and release markers
        const thresholdBandsPlugin = {{
            id: 'thresholdBands',
            beforeDraw: (chart) => {{
                const {{ ctx: context, chartArea, scales, options }} = chart;
                const metric = options.plugins?.thresholdBands?.metric;
                if (!chartArea) return;

                const yScale = scales.y;
                const xScale = scales.x;
                const {{ left, right, top, bottom }} = chartArea;

                context.save();
                // Clip to chart area to prevent overflow
                context.beginPath();
                context.rect(left, top, right - left, bottom - top);
                context.clip();

                // Draw threshold bands for CCN and MI
                if (metric === 'ccn') {{
                    const y5 = yScale.getPixelForValue(5);
                    const y10 = yScale.getPixelForValue(10);
                    // Green zone: 0-5 (at bottom)
                    context.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    context.fillRect(left, y5, right - left, bottom - y5);
                    // Yellow zone: 5-10
                    context.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    context.fillRect(left, y10, right - left, y5 - y10);
                    // Red zone: 10+ (at top)
                    context.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    context.fillRect(left, top, right - left, y10 - top);
                }} else if (metric === 'mi') {{
                    const y80 = yScale.getPixelForValue(80);
                    const y50 = yScale.getPixelForValue(50);
                    // Green zone: 80-100 (at top)
                    context.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    context.fillRect(left, top, right - left, y80 - top);
                    // Yellow zone: 50-80
                    context.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    context.fillRect(left, y80, right - left, y50 - y80);
                    // Red zone: 0-50 (at bottom)
                    context.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    context.fillRect(left, y50, right - left, bottom - y50);
                }} else if (metric === 'antipatterns') {{
                    const y20 = yScale.getPixelForValue(20);
                    const y40 = yScale.getPixelForValue(40);
                    // Green zone: 0-20 (at bottom, lower is better)
                    context.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    context.fillRect(left, y20, right - left, bottom - y20);
                    // Yellow zone: 20-40
                    context.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    context.fillRect(left, y40, right - left, y20 - y40);
                    // Red zone: 40+ (at top)
                    context.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    context.fillRect(left, top, right - left, y40 - top);
                }}

                // Draw major Drupal release markers
                const labels = chart.data.labels || [];
                const releases = [
                    {{ name: 'D7', dates: ['2011-01'] }},
                    {{ name: 'D8', dates: ['2015-10', '2015-11', '2016-01'] }},
                    {{ name: 'D9', dates: ['2020-04', '2020-07'] }},
                    {{ name: 'D10', dates: ['2022-10', '2023-01'] }},
                    {{ name: 'D11', dates: ['2024-07', '2024-10'] }}
                ];

                releases.forEach((release) => {{
                    const index = labels.findIndex(label => release.dates.includes(label));
                    if (index >= 0) {{
                        const x = xScale.getPixelForValue(index);
                        context.strokeStyle = 'rgba(100, 116, 139, 0.4)';
                        context.lineWidth = 1;
                        context.setLineDash([4, 4]);
                        context.beginPath();
                        context.moveTo(x, top);
                        context.lineTo(x, bottom);
                        context.stroke();
                        context.setLineDash([]);

                        context.fillStyle = 'rgba(100, 116, 139, 0.7)';
                        context.font = '10px -apple-system, sans-serif';
                        context.fillText(release.name, x + 3, top + 12);
                    }}
                }});

                context.restore();
            }}
        }};
        Chart.register(thresholdBandsPlugin);

        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        function createChart(container, data, metric, title, subtitle) {{
            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>${{title}}</h2><p class="section-subtitle">${{subtitle}}</p><div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');
            const sortedData = [...data].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);

            const chart = new Chart(context, {{
                type: 'line',
                data: {{
                    labels,
                    datasets: [
                        {{ label: 'Production code', data: sortedData.map(d => d.production?.[metric] || 0), borderColor: colors.production, backgroundColor: colors.production, borderWidth: 2, tension: 0.3, pointRadius: 3 }},
                        {{ label: 'Tests', data: sortedData.map(d => d.test?.[metric] || 0), borderColor: colors.test, backgroundColor: colors.test, borderWidth: 2, tension: 0.3, pointRadius: 3, hidden: true }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ intersect: false, mode: 'index' }},
                    plugins: {{
                        legend: {{ display: true, position: 'top', labels: {{ usePointStyle: true, padding: 15 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                label: tooltipContext => `${{tooltipContext.dataset.label}}: ${{metric === 'loc' ? tooltipContext.parsed.y.toLocaleString() : tooltipContext.parsed.y.toFixed(1)}}`
                            }}
                        }},
                        thresholdBands: {{ metric: metric }}
                    }},
                    scales: {{
                        x: {{
                            grid: {{ display: false }},
                            ticks: {{
                                callback: function(value, index) {{
                                    const label = this.getLabelForValue(value);
                                    const year = label.substring(0, 4);
                                    // Only show year on first occurrence
                                    const labels = this.chart.data.labels;
                                    const prevLabel = index > 0 ? labels[index - 1] : '';
                                    if (prevLabel.substring(0, 4) !== year) {{
                                        return year;
                                    }}
                                    return '';
                                }},
                                maxRotation: 0
                            }}
                        }},
                        y: {{
                            min: 0,
                            max: metric === 'mi' ? 100 : undefined,
                            grid: {{ color: '#e2e8f0' }}
                        }}
                    }}
                }}
            }});
        }}

        function renderHotspots(container, files) {{
            if (!files?.length) return;

            // Filter to production files
            const prodFiles = files.filter(f => !f.test);
            if (!prodFiles.length) return;

            let currentSort = 'ccn';
            const maxShow = 50;
            const initialShow = 15;
            let expanded = false;

            const sortFns = {{
                ccn: (a, b) => (b.ccn || 0) - (a.ccn || 0),
                mi: (a, b) => (a.mi || 100) - (b.mi || 100),  // Lower MI is worse
                antipatterns: (a, b) => (b.antipatterns || 0) - (a.antipatterns || 0)
            }};

            const makeRow = (file, index) => {{
                const gitlabUrl = file.file ? `https://git.drupalcode.org/project/drupal/-/blob/HEAD/core/${{file.file}}` : '#';
                const fileName = file.file ? file.file.split('/').pop() : 'Unknown';
                return `
                    <tr>
                        <td>${{index + 1}}</td>
                        <td class="class-name"><a href="${{gitlabUrl}}" title="${{escapeHtml(file.file)}}">${{escapeHtml(fileName)}}</a></td>
                        <td class="${{file.ccn > 100 ? 'metric-bad' : file.ccn > 50 ? 'metric-warning' : ''}}">${{file.ccn}}</td>
                        <td class="${{file.mi < 50 ? 'metric-bad' : file.mi < 80 ? 'metric-warning' : 'metric-good'}}">${{file.mi}}</td>
                        <td class="${{file.antipatterns > 20 ? 'metric-bad' : file.antipatterns > 10 ? 'metric-warning' : ''}}">${{file.antipatterns || '—'}}</td>
                    </tr>
                `;
            }};

            const renderTable = () => {{
                const sorted = [...prodFiles].sort(sortFns[currentSort]).slice(0, maxShow);
                const showCount = expanded ? sorted.length : Math.min(initialShow, sorted.length);
                const tbody = document.getElementById('hotspots-tbody');
                tbody.innerHTML = sorted.slice(0, showCount).map(makeRow).join('');

                // Update active button
                document.querySelectorAll('.sort-button').forEach(button => {{
                    button.classList.toggle('active', button.dataset.sort === currentSort);
                }});

                // Update toggle button
                const toggleButton = document.getElementById('toggle-hotspots');
                if (toggleButton) {{
                    toggleButton.textContent = expanded ? 'Show fewer' : `Show all ${{sorted.length}} hotspots`;
                    toggleButton.style.display = sorted.length > initialShow ? 'inline-block' : 'none';
                }}
            }};

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `
                <h2>Complexity hotspots</h2>
                <p class="section-subtitle">Production files that may benefit from refactoring. Sort by:
                    <button class="sort-button active" data-sort="ccn">CCN</button>
                    <button class="sort-button" data-sort="mi">MI</button>
                    <button class="sort-button" data-sort="antipatterns">Anti-patterns</button>
                </p>
                <table class="hotspots-table">
                    <thead><tr><th>#</th><th>File</th><th>CCN</th><th>MI</th><th>Anti-patterns</th></tr></thead>
                    <tbody id="hotspots-tbody"></tbody>
                </table>
                <button id="toggle-hotspots" class="toggle-button">Show all</button>
            `;
            container.appendChild(section);

            // Add event listeners for sort buttons
            section.querySelectorAll('.sort-button').forEach(button => {{
                button.addEventListener('click', function() {{
                    currentSort = this.dataset.sort;
                    renderTable();
                }});
            }});

            // Add event listener for toggle button
            document.getElementById('toggle-hotspots').addEventListener('click', function() {{
                expanded = !expanded;
                renderTable();
            }});

            // Initial render
            renderTable();
        }}

        function renderRecentCommits(container, commits) {{
            if (!commits || !commits.length) return;

            const section = document.createElement('div');
            section.className = 'card';
            const initialShow = 15;
            let expanded = false;

            const formatDelta = (value) => {{
                if (value > 0) return `+${{value}}`;
                return (value || 0).toString();
            }};

            const makeRow = (commit) => {{
                const ccnClass = commit.ccnDelta > 0 ? 'metric-bad' : (commit.ccnDelta < 0 ? 'metric-good' : '');
                const miClass = commit.miDelta < 0 ? 'metric-bad' : (commit.miDelta > 0 ? 'metric-good' : '');
                const antipatternClass = commit.antipatternsDelta > 0 ? 'metric-bad' : (commit.antipatternsDelta < 0 ? 'metric-good' : '');
                return `
                    <tr>
                        <td><a href="https://git.drupalcode.org/project/drupal/-/commit/${{commit.hash}}"><code>${{commit.hash}}</code></a></td>
                        <td>${{commit.date}}</td>
                        <td>${{commit.type}}</td>
                        <td>${{escapeHtml(commit.message)}}</td>
                        <td>${{formatDelta(commit.locDelta)}}</td>
                        <td class="${{ccnClass}}">${{formatDelta(commit.ccnDelta)}}</td>
                        <td class="${{miClass}}">${{formatDelta(commit.miDelta)}}</td>
                        <td class="${{antipatternClass}}">${{formatDelta(commit.antipatternsDelta)}}</td>
                    </tr>
                `;
            }};

            const initialRows = commits.slice(0, initialShow).map(makeRow).join('');
            const hasMore = commits.length > initialShow;
            const toggleButton = hasMore ? `<button id="toggle-commits" class="toggle-button">Show all ${{commits.length}} commits</button>` : '';

            section.innerHTML = `
                <h2>Recent commits</h2>
                <p class="section-subtitle">Commits that affected complexity or anti-patterns. Commits with no impact are not shown.</p>
                <table class="hotspots-table">
                    <thead><tr><th>Commit</th><th>Date</th><th>Type</th><th>Message</th><th>LOC Δ</th><th>CCN Δ</th><th>MI Δ</th><th>Anti-patterns Δ</th></tr></thead>
                    <tbody id="commits-tbody">${{initialRows}}</tbody>
                </table>
                ${{toggleButton}}
            `;
            container.appendChild(section);

            if (hasMore) {{
                document.getElementById('toggle-commits').addEventListener('click', function() {{
                    const tbody = document.getElementById('commits-tbody');
                    expanded = !expanded;
                    if (expanded) {{
                        tbody.innerHTML = commits.map(makeRow).join('');
                        this.textContent = 'Show fewer commits';
                    }} else {{
                        tbody.innerHTML = commits.slice(0, initialShow).map(makeRow).join('');
                        this.textContent = `Show all ${{commits.length}} commits`;
                    }}
                }});
            }}
        }}

        function createAntiPatternsChart(container, data) {{
            // Filter to data points that have antipatterns data
            const patternsData = data.filter(d => d.antipatterns && Object.keys(d.antipatterns).length > 0);
            if (patternsData.length < 2) return;

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>Drupal anti-patterns</h2>
                <p class="section-subtitle">Code patterns with known downsides. Tracks occurrences per 1k lines of code.</p>
                <ul class="metric-list">
                    <li><strong>Magic keys</strong>: #-prefixed array keys require memorization and lack IDE support. Inherent to Drupal's render array architecture.</li>
                    <li><strong>Deep arrays</strong>: arrays nested 3+ levels deep are hard to read and refactor</li>
                    <li><strong>Service locators</strong>: <code>\\Drupal::service()</code> calls hide dependencies and hinder testing</li>
                </ul>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');
            const sortedData = [...patternsData].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);

            // Calculate density (per 1k LOC) for each category
            const getDensity = (d, key) => {{
                const count = d.antipatterns?.[key] || 0;
                const loc = d.production?.loc || 1;
                return (count / loc) * 1000;
            }};

            const antipatternColors = {{
                magicKeys: '#ef4444',          // red
                deepArrays: '#f97316',         // orange
                serviceLocators: '#eab308'     // yellow
            }};

            new Chart(context, {{
                type: 'line',
                data: {{
                    labels,
                    datasets: [
                        {{ label: 'Magic keys', data: sortedData.map(d => getDensity(d, 'magicKeys')), borderColor: antipatternColors.magicKeys, backgroundColor: antipatternColors.magicKeys + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Deep arrays', data: sortedData.map(d => getDensity(d, 'deepArrays')), borderColor: antipatternColors.deepArrays, backgroundColor: antipatternColors.deepArrays + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Service locators', data: sortedData.map(d => getDensity(d, 'serviceLocators')), borderColor: antipatternColors.serviceLocators, backgroundColor: antipatternColors.serviceLocators + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ intersect: false, mode: 'index' }},
                    plugins: {{
                        legend: {{ display: true, position: 'top', labels: {{ usePointStyle: true, padding: 15 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                label: tooltipContext => `${{tooltipContext.dataset.label}}: ${{tooltipContext.parsed.y.toFixed(1)}} per 1k LOC`
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            stacked: true,
                            grid: {{ display: false }},
                            ticks: {{
                                callback: function(value, index) {{
                                    const label = this.getLabelForValue(value);
                                    const year = label.substring(0, 4);
                                    const labels = this.chart.data.labels;
                                    const prevLabel = index > 0 ? labels[index - 1] : '';
                                    if (prevLabel.substring(0, 4) !== year) {{
                                        return year;
                                    }}
                                    return '';
                                }},
                                maxRotation: 0
                            }}
                        }},
                        y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: 'Density (per 1k LOC)' }} }}
                    }}
                }}
            }});
        }}

        function createSurfaceAreaChart(container, data) {{
            // Filter to data points that have surfaceArea data
            const saData = data.filter(d => d.surfaceArea && Object.keys(d.surfaceArea).length > 0);
            if (saData.length < 2) return;

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>API surface area</h2>
                <p class="section-subtitle">Tracks distinct extension points in Drupal core. A larger surface area may correlate with a steeper learning curve.</p>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');
            const sortedData = [...saData].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);

            const saColors = {{
                pluginTypes: '#ef4444',     // red
                hooks: '#f97316',           // orange
                magicKeys: '#eab308',       // yellow
                events: '#22c55e',          // green
                services: '#3b82f6',        // blue
                yamlFormats: '#8b5cf6'      // purple
            }};

            new Chart(context, {{
                type: 'line',
                data: {{
                    labels,
                    datasets: [
                        {{ label: 'Plugin types', data: sortedData.map(d => d.surfaceArea?.pluginTypes || 0), borderColor: saColors.pluginTypes, backgroundColor: saColors.pluginTypes + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Hooks', data: sortedData.map(d => d.surfaceArea?.hooks || 0), borderColor: saColors.hooks, backgroundColor: saColors.hooks + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Magic keys', data: sortedData.map(d => d.surfaceArea?.magicKeys || 0), borderColor: saColors.magicKeys, backgroundColor: saColors.magicKeys + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Events', data: sortedData.map(d => d.surfaceArea?.events || 0), borderColor: saColors.events, backgroundColor: saColors.events + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Services', data: sortedData.map(d => d.surfaceArea?.services || 0), borderColor: saColors.services, backgroundColor: saColors.services + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'YAML formats', data: sortedData.map(d => d.surfaceArea?.yamlFormats || 0), borderColor: saColors.yamlFormats, backgroundColor: saColors.yamlFormats + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ intersect: false, mode: 'index' }},
                    plugins: {{
                        legend: {{ display: true, position: 'top', labels: {{ usePointStyle: true, padding: 10 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                footer: (items) => {{
                                    const total = items.reduce((sum, item) => sum + item.parsed.y, 0);
                                    return `Total: ${{total}}`;
                                }}
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            stacked: true,
                            grid: {{ display: false }},
                            ticks: {{
                                callback: function(value, index) {{
                                    const label = this.getLabelForValue(value);
                                    const year = label.substring(0, 4);
                                    const labels = this.chart.data.labels;
                                    const prevLabel = index > 0 ? labels[index - 1] : '';
                                    if (prevLabel.substring(0, 4) !== year) {{
                                        return year;
                                    }}
                                    return '';
                                }},
                                maxRotation: 0
                            }}
                        }},
                        y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: 'Total' }} }}
                    }}
                }}
            }});
        }}

        function createCommitsPerYearChart(container, commitsPerYear) {{
            if (!commitsPerYear || commitsPerYear.length < 2) return;

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>Commits per year</h2>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');

            const labels = commitsPerYear.map(d => d.year.toString());
            const data = commitsPerYear.map(d => d.commits);

            new Chart(context, {{
                type: 'bar',
                data: {{
                    labels,
                    datasets: [{{
                        label: 'Commits',
                        data,
                        backgroundColor: '#3b82f6',
                        borderColor: '#2563eb',
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                label: (tooltipContext) => `${{tooltipContext.parsed.y.toLocaleString()}} commits`
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{ grid: {{ display: false }} }},
                        y: {{ beginAtZero: true, title: {{ display: true, text: 'Commits' }} }}
                    }}
                }}
            }});
        }}

        function createCommitsPerMonthChart(container, commitsMonthly) {{
            if (!commitsMonthly || commitsMonthly.length < 2) return;

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>Commits per month</h2>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');

            // Always show last 4 years for this chart, clustered by month
            const currentYear = new Date().getFullYear();
            const years = [currentYear - 3, currentYear - 2, currentYear - 1, currentYear];
            const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

            // Build data for each year
            const yearColors = ['#cbd5e1', '#94a3b8', '#3b82f6', '#22c55e']; // light gray, gray, blue, green (oldest to newest)
            const datasets = years.map((year, yearIndex) => {{
                const monthData = monthNames.map((_, monthIndex) => {{
                    const monthStr = String(monthIndex + 1).padStart(2, '0');
                    const dateKey = `${{year}}-${{monthStr}}`;
                    const found = commitsMonthly.find(d => d.date === dateKey);
                    return found ? found.total : 0;
                }});
                return {{
                    label: year.toString(),
                    data: monthData,
                    backgroundColor: yearColors[yearIndex],
                    borderColor: yearColors[yearIndex],
                    borderWidth: 1
                }};
            }});

            new Chart(context, {{
                type: 'bar',
                data: {{
                    labels: monthNames,
                    datasets
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: true, position: 'top', labels: {{ usePointStyle: true, padding: 15 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                label: (tooltipContext) => `${{tooltipContext.dataset.label}}: ${{tooltipContext.parsed.y.toLocaleString()}} commits`
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{ grid: {{ display: false }} }},
                        y: {{ beginAtZero: true, title: {{ display: true, text: 'Commits' }} }}
                    }}
                }}
            }});
        }}

        function createCommitTypeDistributionChart(container, commitsMonthly) {{
            if (!commitsMonthly || commitsMonthly.length < 2) return;

            // Show trailing 12 months
            const now = new Date();
            const twelveMonthsAgo = new Date(now.getFullYear(), now.getMonth() - 11, 1).toISOString().slice(0, 7);
            const filtered = commitsMonthly.filter(d => d.date >= twelveMonthsAgo);

            if (filtered.length < 1) return;

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `<h2>Features vs bugs vs maintenance</h2>
                <p class="section-subtitle">Distribution of development work: features, bug fixes, and maintenance (tasks, documentation, tests, CI, performance). Healthy mature projects allocate 20-40% to features. A lower ratio reflects a focus on stability and reliability. Below 20% introduces the risk of the project becoming obsolete. Drupal's architecture encourages innovation in contributed modules, so core's ratio tells only part of the story.</p>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const context = section.querySelector('canvas').getContext('2d');
            const labels = filtered.map(d => d.date);

            // Calculate percentages for 4 categories (including unknown)
            const getPercent = (d, field) => {{
                const total = d.total;
                return total > 0 ? (d[field] / total) * 100 : 0;
            }};

            new Chart(context, {{
                type: 'bar',
                data: {{
                    labels,
                    datasets: [
                        {{
                            label: 'Features',
                            data: filtered.map(d => getPercent(d, 'features')),
                            backgroundColor: '#22c55e',
                            borderColor: '#16a34a',
                            borderWidth: 1
                        }},
                        {{
                            label: 'Bugs',
                            data: filtered.map(d => getPercent(d, 'bugs')),
                            backgroundColor: '#3b82f6',
                            borderColor: '#2563eb',
                            borderWidth: 1
                        }},
                        {{
                            label: 'Maintenance',
                            data: filtered.map(d => getPercent(d, 'maintenance')),
                            backgroundColor: '#60a5fa',
                            borderColor: '#3b82f6',
                            borderWidth: 1
                        }},
                        {{
                            label: 'Unknown',
                            data: filtered.map(d => getPercent(d, 'unknown')),
                            backgroundColor: '#e2e8f0',
                            borderColor: '#cbd5e1',
                            borderWidth: 1
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: true, position: 'top', labels: {{ usePointStyle: true, padding: 15 }} }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12,
                            callbacks: {{
                                label: (tooltipContext) => `${{tooltipContext.dataset.label}}: ${{tooltipContext.parsed.y.toFixed(1)}}%`
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            stacked: true,
                            grid: {{ display: false }},
                            ticks: {{ maxRotation: 0 }}
                        }},
                        y: {{
                            stacked: true,
                            beginAtZero: true,
                            max: 100,
                            title: {{ display: true, text: 'Percentage' }},
                            ticks: {{
                                callback: (value) => value + '%'
                            }}
                        }}
                    }}
                }}
            }});
        }}

        function renderDashboard(snapshots, commits, commitsPerYear, files, surfaceAreaLists, commitsMonthly) {{
            const container = document.getElementById('dashboard');
            container.innerHTML = '';
            if (!snapshots.length) {{
                container.innerHTML = '<div class="error">No data available.</div>';
                return;
            }}
            // Charts
            createChart(container, snapshots, 'loc', 'Lines of code', 'Drupal core is a large codebase, too big for any one person to fully understand, but manageable for a healthy community. For scale, Kubernetes is about 10x larger and the Linux kernel about 75x. A healthy test-to-production ratio is 1:1 or higher, and Drupal far exceeds this. Measures non-blank, non-comment lines (SLOC), excluding vendor dependencies.');
            createChart(container, snapshots, 'ccn', 'Cyclomatic complexity (CCN)', '<a href="https://en.wikipedia.org/wiki/Cyclomatic_complexity">Cyclomatic complexity</a> measures decision paths. Green (&lt;5) simple, yellow (5-10) moderate, red (&gt;10) complex.');
            createChart(container, snapshots, 'mi', 'Maintainability index (MI)', '<a href="https://www.verifysoft.com/en_maintainability.html">Maintainability index</a> (0-100). Green (&gt;80) clean, yellow (50-80) review, red (&lt;50) hard to maintain.');
            createAntiPatternsChart(container, snapshots);
            createSurfaceAreaChart(container, snapshots);
            // Recent commits
            renderRecentCommits(container, commits);
            // Hotspots
            renderHotspots(container, files);
            // Commit activity charts
            createCommitsPerYearChart(container, commitsPerYear);
            createCommitsPerMonthChart(container, commitsMonthly);
            createCommitTypeDistributionChart(container, commitsMonthly);
            // Surface area reference
            renderSurfaceAreaReference(container, surfaceAreaLists);
        }}

        function renderSurfaceAreaReference(container, surfaceAreaLists) {{
            if (!surfaceAreaLists || !Object.keys(surfaceAreaLists).length) return;

            const lists = surfaceAreaLists;
            const titles = {{
                pluginTypes: 'Plugin types',
                hooks: 'Hooks',
                magicKeys: 'Magic keys',
                events: 'Events',
                services: 'Services',
                yamlFormats: 'YAML formats'
            }};

            const panels = Object.entries(lists)
                .filter(([key]) => titles[key])  // Only show categories with defined titles
                .map(([key, items]) => {{
                    const sorted = [...items].sort();
                    return `
                        <div class="sa-panel">
                            <div class="sa-panel-header">${{titles[key]}} (${{items.length}})</div>
                            <div class="sa-list">${{sorted.map(item => `<div>${{item}}</div>`).join('')}}</div>
                        </div>
                    `;
                }}).join('');

            const section = document.createElement('div');
            section.className = 'card';
            section.innerHTML = `
                <h2>API surface details</h2>
                <div class="surface-area-grid">${{panels}}</div>
            `;
            container.appendChild(section);
        }}

        DATA.snapshots.sort((a, b) => b.date.localeCompare(a.date));

        // Render dashboard
        renderDashboard(DATA.snapshots, DATA.commits, DATA.commitsPerYear, DATA.files, DATA.surfaceAreaLists, DATA.commitsMonthly);
    </script>
</body>
</html>'''

    with open(html_file, "w") as f:
        f.write(html_content)

    log_info(f"Generated {html_file}")


def analyze_version(drupal_dir: Path, commit: str, year_month: str,
                    output_dir: Path, current: int = 0, total: int = 0) -> Optional[dict]:
    """Analyze a single version of Drupal using drupalisms.php.

    Returns a snapshot dict with production, test, and files data.
    """
    work_dir = output_dir / "work"

    progress = f" [{current}/{total}]" if total else ""
    log_info(f"Analyzing {year_month} (commit: {commit[:8]}){progress}")

    if not export_version(drupal_dir, commit, work_dir):
        return None

    # Only analyze D8+ with core/ directory
    if not (work_dir / "core").is_dir():
        log_warn(f"No core/ directory for {year_month}, skipping")
        return None

    # Run drupalisms.php for all metrics
    scripts_dir = Path(__file__).parent
    php_script = scripts_dir / "drupalisms.php"

    try:
        result = subprocess.run(
            ["php", "-d", "memory_limit=2G", str(php_script), str(work_dir / "core")],
            capture_output=True,
            text=True,
            timeout=600
        )
        if result.returncode != 0:
            log_warn(f"drupalisms.php failed for {year_month}")
            return None

        data = json.loads(result.stdout)

        return {
            "date": year_month,
            "commit": commit[:8],
            "production": data["production"],
            "test": data["test"],
            "surfaceArea": data.get("surfaceArea", {}),
            "surfaceAreaLists": data.get("surfaceAreaLists", {}),
            "antipatterns": data.get("antipatterns", {}),
            "files": data["files"],
        }
    except Exception as e:
        log_warn(f"Analysis failed for {year_month}: {e}")
        return None


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
    log_info("Analyzing current HEAD...")
    code, head_commit, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(drupal_dir))
    if code == 0 and head_commit.strip():
        current_date = datetime.now().strftime("%Y-%m")
        # Only add if not already covered by the last snapshot
        if not snapshots or snapshots[-1]["date"] != current_date:
            result = analyze_version(drupal_dir, head_commit.strip(), current_date, output_dir)
            if result:
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

    # Extract files and surfaceAreaLists from latest snapshot (only needed once)
    latest = snapshots[-1] if snapshots else {}
    files = latest.pop("files", [])
    surfaceAreaLists = latest.pop("surfaceAreaLists", {})

    # Strip from all snapshots
    for snapshot in snapshots:
        snapshot.pop("files", None)
        snapshot.pop("surfaceAreaLists", None)

    # Build final data structure
    data = {
        "generated": datetime.now().isoformat(),
        "commitsMonthly": commitsMonthly,
        "snapshots": snapshots,
        "commits": commits,
        "commitsPerYear": commitsPerYear,
        "files": files,
        "surfaceAreaLists": surfaceAreaLists,
    }

    # Save results as JSON
    with open(data_file, "w") as f:
        json.dump(data, f, indent=2)

    # Generate index.html with embedded data
    generate_html(project_dir, data)

    log_info(f"Analysis complete! Processed {len(snapshots)} snapshots.")
    log_info(f"Data saved to: {data_file}")
    log_info(f"Dashboard saved to: {project_dir / 'index.html'}")


if __name__ == "__main__":
    main()
