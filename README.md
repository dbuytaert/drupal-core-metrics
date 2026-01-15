# Drupal Core Metrics

A dashboard that tracks Drupal core's codebase over time: lines of code, complexity, maintainability, anti-patterns, and API surface area.

**View the dashboard:** https://dbuytaert.github.io/drupal-core-metrics/

**Learn more:** [Measuring Drupal Core code complexity](https://dri.es/measuring-drupal-core-code-complexity)


## Metrics

### Code quality
- **SLOC**: Source lines of code (excluding blanks and comments)
- **Cyclomatic complexity**: Decision paths in code. Lower is simpler.
- **Maintainability index**: 0-100 score. Higher is easier to maintain.

### Anti-patterns
Code patterns with known downsides. Tracked per 1k lines.

| Pattern | Description |
|---------|-------------|
| Magic keys | `#`-prefixed array keys. Inherent to Drupal's render array architecture. |
| Deep arrays | 3+ levels of nesting. Hard to read and refactor. |
| Service locators | `\Drupal::service()` calls. Hides dependencies, hinders testing. |

### API surface area
Distinct extension points in Drupal. A larger surface may correlate with a steeper learning curve.

| Category | Examples |
|----------|----------|
| Plugin types | Block, Field, ViewsDisplay |
| Hooks | hook_form_alter, hook_entity_presave |
| Magic keys | #theme, #states, #ajax (vocabulary size) |
| Events | KernelEvents::REQUEST |
| Services | cache, entity, router |
| YAML formats | routing, permissions, services |


## Running locally

### Option 1: DDEV (Recommended)

**Prerequisites:** [DDEV](https://ddev.readthedocs.io/en/stable/) installed

```bash
# 1. Clone and start DDEV
git clone https://github.com/dbuytaert/drupal-core-metrics.git
cd drupal-core-metrics
ddev start

# 2. Install PHP dependencies
ddev composer install

# 3. Run analysis (15-30 min)
ddev exec "cd /var/www/html && python3 scripts/analyze.py"

# 4. View dashboard
ddev launch
```

For verbose output during analysis:
```bash
ddev exec "cd /var/www/html && DEBUG=1 python3 scripts/analyze.py"
```

### Option 2: Manual Setup

**Prerequisites:** PHP 8.1+, Python 3, Composer

```bash
composer install              # Install dependencies
python3 scripts/analyze.py    # Run analysis (15-30 min)
```

This generates `data.json`. The `index.html` file is static and does not need to be regenerated.

### Viewing the dashboard (manual)

The dashboard loads data via `fetch()`, which requires an HTTP server (browsers block this for local files). Start a simple server:

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000 in your browser.


## Contributing

Questions or ideas? Open an issue or PR.
