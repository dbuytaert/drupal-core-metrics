#!/usr/bin/env python3
"""
Drupal Core Dashboard - Data Collection Script

Analyzes Drupal core across historical snapshots, collecting metrics like
LOC, CCN, MI, anti-patterns, and concepts. Uses drupalisms.php for all analysis.
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


def log_info(msg: str):
    print(f"{Colors.GREEN}[INFO]{Colors.NC} {msg}", flush=True)


def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}", flush=True)


def log_error(msg: str):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}", flush=True)


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
        code, _, err = run_command(["git", "fetch", "--all", "--tags"], cwd=str(drupal_dir))
        if code != 0:
            log_error(f"Failed to fetch updates: {err}")
            return False
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


def get_recent_commits(drupal_dir: Path, min_lines: int = 50, days: int = 60) -> list[dict]:
    """Get recent commits with at least min_lines changed.

    Returns list of {hash, message, date, lines} sorted by date descending.
    """
    # Get commit log with stats (%cs = short date like 2025-12-26)
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
            if total >= min_lines:
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
                    'sort_date': current_date,  # Keep original for sorting
                    'lines': total
                })
            current_hash = None

    # Sort by date descending (most recent first)
    commits = sorted(commits, key=lambda x: x['sort_date'], reverse=True)
    # Remove sort_date from output
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
                           min_lines: int = 50, days: int = 60) -> list[dict]:
    """Analyze metric deltas for recent significant commits.

    Returns list of commits with their metric deltas.
    """
    commits = get_recent_commits(drupal_dir, min_lines, days)
    if not commits:
        return []

    log_info(f"Analyzing {len(commits)} recent commits")

    work_dir = output_dir / "commit_work"
    results = []

    for commit in commits:
        log_info(f"Analyzing commit {commit['hash'][:11]}")

        delta = analyze_commit_delta(drupal_dir, commit['hash'], work_dir)
        if delta:
            results.append({
                "hash": commit['hash'][:11],
                "date": commit['date'],
                "message": commit['message'],
                "locDelta": delta['locDelta'],
                "ccnDelta": delta['ccnDelta'],
                "miDelta": delta['miDelta'],
                "antipatternsDelta": delta['antipatternsDelta'],
            })

    # Cleanup work directory
    if work_dir.exists():
        shutil.rmtree(work_dir)

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
    <meta name="description" content="Track Drupal core's code quality over time. See lines of code, cyclomatic complexity, maintainability index, anti-patterns, and concepts to learn.">
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
            padding: 2rem;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        header {{ margin-bottom: 2rem; }}
        h1 {{ font-size: 1.75rem; font-weight: 600; margin-bottom: 0.5rem; }}
        .header-meta {{ font-size: 0.875rem; color: var(--text-secondary); }}
        .chart-section {{
            background: var(--bg-primary);
            border-radius: 0.75rem;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}
        .chart-section h2 {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 0.25rem; }}
        .chart-section .chart-subtitle {{ font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.75rem; }}
        .chart-section .metric-list {{ font-size: 0.8125rem; color: var(--text-secondary); margin: 0 0 1rem 0; padding-left: 1.25rem; line-height: 1.6; }}
        .chart-section .metric-list li {{ margin-bottom: 0.125rem; }}
        .chart-section .metric-list strong {{ color: var(--text-primary); font-weight: 500; }}
        .chart-container {{ position: relative; height: 300px; }}
        .hotspots-section, .changes-section {{
            background: var(--bg-primary);
            border-radius: 0.75rem;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
        }}
        .hotspots-section h2, .changes-section h2 {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 0.25rem; }}
        .hotspots-section .section-subtitle, .changes-section .section-subtitle {{ font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 1rem; }}
        .hotspots-table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; }}
        .hotspots-table th {{
            text-align: left;
            padding: 0.75rem;
            border-bottom: 2px solid var(--border-color);
            font-weight: 600;
            color: var(--text-secondary);
        }}
        .hotspots-table td {{ padding: 0.75rem; border-bottom: 1px solid var(--border-color); }}
        .hotspots-table tr:hover {{ background: var(--bg-secondary); }}
        .hotspots-table .class-name {{ font-family: 'SF Mono', Monaco, 'Courier New', monospace; font-size: 0.8125rem; }}
        .hotspots-table .metric-bad {{ color: var(--color-bad); font-weight: 600; }}
        .hotspots-table .metric-warning {{ color: var(--color-warning); font-weight: 600; }}
        .hotspots-table .metric-good {{ color: var(--color-good); }}
        .toggle-button {{ margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; border: 1px solid var(--border-color); border-radius: 0.375rem; background: var(--bg-primary); }}
        .toggle-button:hover {{ background: var(--bg-secondary); }}
        .concepts-reference {{ background: var(--bg-primary); border-radius: 0.75rem; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1); }}
        .concepts-reference h2 {{ font-size: 1.125rem; font-weight: 600; margin-bottom: 0.25rem; }}
        .concepts-reference .section-subtitle {{ font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 1rem; }}
        .concepts-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 1rem; }}
        .concept-panel {{ border: 1px solid var(--border-color); border-radius: 0.5rem; overflow: hidden; }}
        .concept-panel summary {{ padding: 0.75rem 1rem; background: var(--bg-secondary); cursor: pointer; font-weight: 500; font-size: 0.875rem; }}
        .concept-panel summary:hover {{ background: #e2e8f0; }}
        .concept-panel .concept-list {{ padding: 0.75rem 1rem; max-height: 200px; overflow-y: auto; font-size: 0.8125rem; font-family: 'SF Mono', Monaco, 'Courier New', monospace; }}
        .concept-panel .concept-list div {{ padding: 0.125rem 0; color: var(--text-secondary); }}
        .error {{ text-align: center; padding: 2rem; color: var(--color-bad); background: #fef2f2; border-radius: 0.5rem; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Drupal Core Metrics</h1>
            <p class="header-meta">Last updated: {latest_date}</p>
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
                const {{ ctx, chartArea, scales, options }} = chart;
                const metric = options.plugins?.thresholdBands?.metric;
                if (!chartArea) return;

                const yScale = scales.y;
                const xScale = scales.x;
                const {{ left, right, top, bottom }} = chartArea;

                ctx.save();
                // Clip to chart area to prevent overflow
                ctx.beginPath();
                ctx.rect(left, top, right - left, bottom - top);
                ctx.clip();

                // Draw threshold bands for CCN and MI
                if (metric === 'ccn') {{
                    const y5 = yScale.getPixelForValue(5);
                    const y10 = yScale.getPixelForValue(10);
                    // Green zone: 0-5 (at bottom)
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    ctx.fillRect(left, y5, right - left, bottom - y5);
                    // Yellow zone: 5-10
                    ctx.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    ctx.fillRect(left, y10, right - left, y5 - y10);
                    // Red zone: 10+ (at top)
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    ctx.fillRect(left, top, right - left, y10 - top);
                }} else if (metric === 'mi') {{
                    const y80 = yScale.getPixelForValue(80);
                    const y50 = yScale.getPixelForValue(50);
                    // Green zone: 80-100 (at top)
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    ctx.fillRect(left, top, right - left, y80 - top);
                    // Yellow zone: 50-80
                    ctx.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    ctx.fillRect(left, y80, right - left, y50 - y80);
                    // Red zone: 0-50 (at bottom)
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    ctx.fillRect(left, y50, right - left, bottom - y50);
                }} else if (metric === 'antipatterns') {{
                    const y20 = yScale.getPixelForValue(20);
                    const y40 = yScale.getPixelForValue(40);
                    // Green zone: 0-20 (at bottom, lower is better)
                    ctx.fillStyle = 'rgba(34, 197, 94, 0.15)';
                    ctx.fillRect(left, y20, right - left, bottom - y20);
                    // Yellow zone: 20-40
                    ctx.fillStyle = 'rgba(245, 158, 11, 0.15)';
                    ctx.fillRect(left, y40, right - left, y20 - y40);
                    // Red zone: 40+ (at top)
                    ctx.fillStyle = 'rgba(239, 68, 68, 0.15)';
                    ctx.fillRect(left, top, right - left, y40 - top);
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

                releases.forEach((release, i) => {{
                    const idx = labels.findIndex(l => release.dates.includes(l));
                    if (idx >= 0) {{
                        const x = xScale.getPixelForValue(idx);
                        ctx.strokeStyle = 'rgba(100, 116, 139, 0.4)';
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 4]);
                        ctx.beginPath();
                        ctx.moveTo(x, top);
                        ctx.lineTo(x, bottom);
                        ctx.stroke();
                        ctx.setLineDash([]);

                        ctx.fillStyle = 'rgba(100, 116, 139, 0.7)';
                        ctx.font = '10px -apple-system, sans-serif';
                        ctx.fillText(release.name, x + 3, top + 12);
                    }}
                }});

                ctx.restore();
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
            section.className = 'chart-section';
            section.innerHTML = `<h2>${{title}}</h2><p class="chart-subtitle">${{subtitle}}</p><div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const ctx = section.querySelector('canvas').getContext('2d');
            const sortedData = [...data].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);
            
            const chart = new Chart(ctx, {{
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
                                label: ctx => `${{ctx.dataset.label}}: ${{metric === 'loc' ? ctx.parsed.y.toLocaleString() : ctx.parsed.y.toFixed(1)}}`
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

        function renderHotspots(container, snapshots) {{
            const latest = snapshots[0];
            if (!latest?.files?.length) return;

            // Filter to production files, sort by combined score (CCN + antipatterns), take top 50
            const hotspots = latest.files
                .filter(f => !f.test)
                .map(f => ({{ ...f, score: (f.ccn || 0) + (f.antipatterns || 0) }}))
                .sort((a, b) => b.score - a.score)
                .slice(0, 50);

            if (!hotspots.length) return;

            const initialShow = 15;
            let expanded = false;

            const makeRow = (f, i) => {{
                const gitlabUrl = f.file ? `https://git.drupalcode.org/project/drupal/-/blob/11.x/core/${{f.file}}` : '#';
                const fileName = f.file ? f.file.split('/').pop() : 'Unknown';
                return `
                    <tr>
                        <td>${{i + 1}}</td>
                        <td class="class-name"><a href="${{gitlabUrl}}" title="${{escapeHtml(f.file)}}">${{escapeHtml(fileName)}}</a></td>
                        <td class="${{f.ccn > 100 ? 'metric-bad' : f.ccn > 50 ? 'metric-warning' : ''}}">${{f.ccn}}</td>
                        <td class="${{f.mi < 50 ? 'metric-bad' : f.mi < 80 ? 'metric-warning' : 'metric-good'}}">${{f.mi}}</td>
                        <td class="${{f.antipatterns > 20 ? 'metric-bad' : f.antipatterns > 10 ? 'metric-warning' : ''}}">${{f.antipatterns || '—'}}</td>
                    </tr>
                `;
            }};

            const initialRows = hotspots.slice(0, initialShow).map(makeRow).join('');
            const hasMore = hotspots.length > initialShow;
            const toggleButton = hasMore ? `<button id="toggle-hotspots" class="toggle-button">Show all ${{hotspots.length}} hotspots</button>` : '';

            const section = document.createElement('div');
            section.className = 'hotspots-section';
            section.innerHTML = `
                <h2>Complexity hotspots</h2>
                <p class="section-subtitle">Production files ranked by complexity (CCN) and anti-pattern density. These files may benefit from refactoring.</p>
                <table class="hotspots-table">
                    <thead><tr><th>#</th><th>File</th><th>CCN</th><th>MI</th><th>Anti-patterns</th></tr></thead>
                    <tbody id="hotspots-tbody">${{initialRows}}</tbody>
                </table>
                ${{toggleButton}}
            `;
            container.appendChild(section);

            if (hasMore) {{
                document.getElementById('toggle-hotspots').addEventListener('click', function() {{
                    const tbody = document.getElementById('hotspots-tbody');
                    expanded = !expanded;
                    if (expanded) {{
                        tbody.innerHTML = hotspots.map(makeRow).join('');
                        this.textContent = 'Show fewer hotspots';
                    }} else {{
                        tbody.innerHTML = hotspots.slice(0, initialShow).map(makeRow).join('');
                        this.textContent = `Show all ${{hotspots.length}} hotspots`;
                    }}
                }});
            }}
        }}

        function renderRecentCommits(container, commits) {{
            if (!commits || !commits.length) return;

            const section = document.createElement('div');
            section.className = 'changes-section';
            const initialShow = 15;
            let expanded = false;

            const formatDelta = (val) => {{
                if (val > 0) return `+${{val}}`;
                return (val || 0).toString();
            }};

            const makeRow = (c) => {{
                const ccnClass = c.ccnDelta > 0 ? 'metric-bad' : (c.ccnDelta < 0 ? 'metric-good' : '');
                const miClass = c.miDelta < 0 ? 'metric-bad' : (c.miDelta > 0 ? 'metric-good' : '');
                const antipatternClass = c.antipatternsDelta > 0 ? 'metric-bad' : (c.antipatternsDelta < 0 ? 'metric-good' : '');
                return `
                    <tr>
                        <td><a href="https://git.drupalcode.org/project/drupal/-/commit/${{c.hash}}"><code>${{c.hash}}</code></a></td>
                        <td>${{c.date}}</td>
                        <td>${{escapeHtml(c.message)}}</td>
                        <td>${{formatDelta(c.locDelta)}}</td>
                        <td class="${{ccnClass}}">${{formatDelta(c.ccnDelta)}}</td>
                        <td class="${{miClass}}">${{formatDelta(c.miDelta)}}</td>
                        <td class="${{antipatternClass}}">${{formatDelta(c.antipatternsDelta)}}</td>
                    </tr>
                `;
            }};

            const initialRows = commits.slice(0, initialShow).map(makeRow).join('');
            const hasMore = commits.length > initialShow;
            const toggleButton = hasMore ? `<button id="toggle-commits" class="toggle-button">Show all ${{commits.length}} commits</button>` : '';

            section.innerHTML = `
                <h2>Recent commits</h2>
                <p class="section-subtitle">Commits with 50+ lines changed in the last 60 days, showing how each affected LOC, complexity, and anti-patterns.</p>
                <table class="hotspots-table">
                    <thead><tr><th>Commit</th><th>Date</th><th>Message</th><th>LOC Δ</th><th>CCN Δ</th><th>MI Δ</th><th>Anti-patterns Δ</th></tr></thead>
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
            const patternsData = data.filter(d => d.production?.antipatterns !== undefined);
            if (patternsData.length < 2) return;

            const section = document.createElement('div');
            section.className = 'chart-section';
            section.innerHTML = `<h2>Drupal anti-patterns</h2>
                <p class="chart-subtitle">Practices that make code harder to understand, test, and maintain. Lower density is better.</p>
                <ul class="metric-list">
                    <li><strong>Service Locator</strong>: using \\Drupal::service() instead of injecting dependencies makes code harder to test and understand</li>
                    <li><strong>Global State</strong>: drupal_static() stores hidden shared state that causes unpredictable side effects</li>
                    <li><strong>Deep Nesting</strong>: arrays nested 3+ levels deep are hard to read, debug, and safely modify</li>
                </ul>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const ctx = section.querySelector('canvas').getContext('2d');
            const sortedData = [...patternsData].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);

            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels,
                    datasets: [
                        {{ label: 'Production', data: sortedData.map(d => d.production?.antipatterns || 0), borderColor: colors.production, backgroundColor: colors.production, borderWidth: 2, tension: 0.3, pointRadius: 3 }},
                        {{ label: 'Tests', data: sortedData.map(d => d.test?.antipatterns || 0), borderColor: colors.test, backgroundColor: colors.test, borderWidth: 2, tension: 0.3, pointRadius: 3, hidden: true }}
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
                                label: ctx => `${{ctx.dataset.label}}: ${{ctx.parsed.y.toFixed(1)}}`
                            }}
                        }},
                        thresholdBands: {{ metric: 'antipatterns' }}
                    }},
                    scales: {{
                        x: {{
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
                        y: {{ beginAtZero: true, title: {{ display: true, text: 'Density (per 1k LOC)' }} }}
                    }}
                }}
            }});
        }}

        function createConceptsChart(container, data) {{
            // Filter to data points that have concepts data
            const conceptsData = data.filter(d => d.concepts && Object.keys(d.concepts).length > 0);
            if (conceptsData.length < 2) return;

            const section = document.createElement('div');
            section.className = 'chart-section';
            section.innerHTML = `<h2>Drupal concepts to learn</h2>
                <p class="chart-subtitle">Distinct concepts a developer may encounter. More concepts means a steeper learning curve.</p>
                <ul class="metric-list">
                    <li><strong>Plugin types</strong>: distinct plugin systems (Block, Field, Action, etc.)</li>
                    <li><strong>Hooks</strong>: callback patterns (form_alter, entity_presave, etc.)</li>
                    <li><strong>Magic keys</strong>: #-prefixed render array keys with special behavior</li>
                    <li><strong>Events</strong>: Symfony events subscribed to</li>
                    <li><strong>Attributes</strong>: PHP attributes for plugins and hooks</li>
                </ul>
                <div class="chart-container"><canvas></canvas></div>`;
            container.appendChild(section);

            const ctx = section.querySelector('canvas').getContext('2d');
            const sortedData = [...conceptsData].sort((a, b) => a.date.localeCompare(b.date));
            const labels = sortedData.map(d => d.date);

            const conceptColors = {{
                pluginTypes: '#ef4444',     // red
                hooks: '#f97316',           // orange
                magicKeys: '#eab308',       // yellow
                events: '#22c55e',          // green
                attributes: '#3b82f6'       // blue
            }};

            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels,
                    datasets: [
                        {{ label: 'Plugin types', data: sortedData.map(d => d.concepts?.pluginTypes || 0), borderColor: conceptColors.pluginTypes, backgroundColor: conceptColors.pluginTypes + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Hooks', data: sortedData.map(d => d.concepts?.hooks || 0), borderColor: conceptColors.hooks, backgroundColor: conceptColors.hooks + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Magic keys', data: sortedData.map(d => d.concepts?.magicKeys || 0), borderColor: conceptColors.magicKeys, backgroundColor: conceptColors.magicKeys + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Events', data: sortedData.map(d => d.concepts?.events || 0), borderColor: conceptColors.events, backgroundColor: conceptColors.events + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }},
                        {{ label: 'Attributes', data: sortedData.map(d => d.concepts?.attributes || 0), borderColor: conceptColors.attributes, backgroundColor: conceptColors.attributes + '80', fill: true, borderWidth: 2, tension: 0.3, pointRadius: 2 }}
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
                                    return `Total: ${{total}} concepts`;
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
                        y: {{ stacked: true, beginAtZero: true, title: {{ display: true, text: 'Total concepts' }} }}
                    }}
                }}
            }});
        }}

        function renderDashboard(snapshots, commits) {{
            const container = document.getElementById('dashboard');
            container.innerHTML = '';
            if (!snapshots.length) {{
                container.innerHTML = '<div class="error">No data available.</div>';
                return;
            }}
            // Charts
            createChart(container, snapshots, 'loc', 'Lines of code', 'Non-blank, non-comment lines (SLOC). Excludes vendor directory. Click legend to toggle test code.');
            createChart(container, snapshots, 'ccn', 'Cyclomatic complexity (CCN)', '<a href="https://en.wikipedia.org/wiki/Cyclomatic_complexity">Cyclomatic complexity</a> measures decision paths. Green (&lt;5) simple, yellow (5-10) moderate, red (&gt;10) complex.');
            createChart(container, snapshots, 'mi', 'Maintainability index (MI)', '<a href="https://www.verifysoft.com/en_maintainability.html">Maintainability index</a> (0-100). Green (&gt;80) clean, yellow (50-80) review, red (&lt;50) hard to maintain.');
            createAntiPatternsChart(container, snapshots);
            createConceptsChart(container, snapshots);
            // Recent commits
            renderRecentCommits(container, commits);
            // Hotspots
            renderHotspots(container, snapshots);
            // Concepts reference
            renderConceptsReference(container, snapshots);
        }}

        function renderConceptsReference(container, snapshots) {{
            const latest = snapshots[0];
            if (!latest?.conceptLists) return;

            const lists = latest.conceptLists;
            const titles = {{
                pluginTypes: 'Plugin types',
                hooks: 'Hooks',
                magicKeys: 'Magic keys',
                events: 'Events',
                attributes: 'Attributes'
            }};

            const panels = Object.entries(lists).map(([key, items]) => {{
                const sorted = [...items].sort();
                return `
                    <details class="concept-panel">
                        <summary>${{titles[key]}} (${{items.length}})</summary>
                        <div class="concept-list">${{sorted.map(i => `<div>${{i}}</div>`).join('')}}</div>
                    </details>
                `;
            }}).join('');

            const section = document.createElement('div');
            section.className = 'concepts-reference';
            section.innerHTML = `
                <h2>Concept reference</h2>
                <p class="section-subtitle">All distinct concepts detected in the current codebase. Click to expand each category.</p>
                <div class="concepts-grid">${{panels}}</div>
            `;
            container.appendChild(section);
        }}

        DATA.snapshots.sort((a, b) => b.date.localeCompare(a.date));
        renderDashboard(DATA.snapshots, DATA.commits);
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
            "concepts": data.get("concepts", {}),
            "conceptLists": data.get("conceptLists", {}),
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

    # Build final data structure
    data = {
        "generated": datetime.now().isoformat(),
        "snapshots": snapshots,
        "commits": commits,
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
