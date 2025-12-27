Drupal Core Metrics is a lightweight dashboard that tracks how Drupal core's codebase changes over time. The scripts in this repository collect common metrics such as [SLOC](https://en.wikipedia.org/wiki/Source_lines_of_code) (source lines of code, excluding blanks and comments), [cyclomatic complexity](https://en.wikipedia.org/wiki/Cyclomatic_complexity), and [maintainability index](https://www.verifysoft.com/en_maintainability.html). It also tracks two Drupal-specific signals: anti-patterns (code we want to reduce) and concepts to learn (the total learning burden for newcomers).

You can explore the dashboard at https://dbuytaert.github.io/drupal-core-metrics/.


## Drupal anti-patterns

Anti-patterns are code patterns we actively want to reduce. These are objectively worse practices that make the codebase harder to maintain and test.

| Category | What we detect | Why it's problematic |
|----------|----------------|---------------------|
| **Service Locator** | Static calls like `\Drupal::service()`, `\Drupal::config()` | Hides dependencies, makes code harder to test, bypasses dependency injection |
| **Global State** | Calls to `drupal_static()` and `drupal_static_reset()` | Hidden shared mutable state; unpredictable side effects; hard to test |
| **Deep Nesting** | Array access/literals beyond 2 levels | Deeply nested arrays are hard to read and refactor |

The dashboard tracks anti-pattern density (per 1,000 lines of code) over time. Lower is better.


## Drupal concepts to learn

We track the number of distinct concepts a developer may encounter when working in different parts of Drupal core. More concepts means a steeper learning curve. These aren't necessarily "bad" - they represent the variety of systems in the codebase.

| Category | What we count | Examples |
|----------|---------------|----------|
| **Plugin types** | Distinct plugin systems | Block, Field, ViewsDisplay, Action, Condition |
| **Hooks** | Distinct hook names | hook_form_alter, hook_entity_presave, hook_preprocess_* |
| **Magic keys** | Distinct #-prefixed render array keys | #lazy_builder, #ajax, #states, #pre_render |
| **Events** | Distinct Symfony events | KernelEvents::REQUEST, EntityEvents::INSERT |
| **Attributes** | Distinct PHP attributes | #[Block], #[Hook], #[ContentEntityType] |

The dashboard tracks each category over time. Growth indicates an expanding learning burden.


## Getting started

### Prerequisites

- **PHP 8.1+** (8.4 recommended)
- **Python 3**
- **Composer** (to install PHP dependencies)

### Running the analysis

```bash
composer install              # Install PHP dependencies (nikic/php-parser)
python3 scripts/analyze.py    # Run the full analysis (takes 15-30 minutes)
open index.html               # View the dashboard
```

### What gets generated

| File | Description |
|------|-------------|
| `drupal-core/` | Bare git clone of Drupal core (created on first run, ~500MB) |
| `data.json` | Historical metrics for all analyzed snapshots |
| `index.html` | The dashboard with embedded charts and data |

### How it works

The analysis script:
1. Clones Drupal core (or fetches updates if already present)
2. Exports 30 semi-annual snapshots from 2011 to present
3. Runs `drupalisms.php` on each snapshot to compute metrics (LOC, CCN, MI), anti-patterns, and concepts
4. Analyzes recent commits (last 60 days) for per-commit metric changes
5. Generates `data.json` and `index.html` with the results

### Data format

The `data.json` file contains all metrics for external analysis:

```json
{
  "generated": "2025-12-27T10:30:00",
  "snapshots": [
    {
      "date": "2025-12",
      "commit": "8adf8b75",
      "production": { "loc": 354584, "ccn": 7.9, "mi": 46.4, "antipatterns": 25.3 },
      "test": { "loc": 301234, "ccn": 4.1, "mi": 78.3, "antipatterns": 8.1 },
      "concepts": {
        "pluginTypes": 51,
        "hooks": 570,
        "magicKeys": 336,
        "events": 9,
        "attributes": 101
      },
      "conceptLists": {
        "pluginTypes": ["ActionManager", "BlockManager", "..."],
        "hooks": ["hook_form_alter", "hook_entity_presave", "..."],
        "magicKeys": ["#theme", "#states", "#ajax", "..."],
        "events": ["KernelEvents::REQUEST", "..."],
        "attributes": ["#[Block]", "#[Hook]", "..."]
      },
      "files": [
        { "file": "modules/views/src/Plugin/views/display/DisplayPluginBase.php", "test": false, "loc": 2500, "ccn": 358, "mi": 36, "antipatterns": 45 }
      ]
    }
  ],
  "commits": [
    { "hash": "8adf8b7508d", "date": "Dec 26, 2025", "message": "Fix Views caching", "locDelta": 50, "ccnDelta": -2, "miDelta": 3, "antipatternsDelta": -5 }
  ]
}
```

- **snapshots**: Semi-annual metrics from 2011 to present (D8+ only, since earlier versions lack the `core/` directory)
- **concepts**: Codebase-level counts of distinct pattern types (not per-file)
- **conceptLists**: Full arrays of concept names for each type (for reference/display)
- **commits**: Recent commits (60 days) with metric deltas for changed files


## Contributing

Questions, ideas, or fixes? Open an issue or PR so we can keep improving Drupal core's visibility into code quality.
