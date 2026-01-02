# Drupal Core Metrics

A dashboard that tracks Drupal core's codebase over time: lines of code, complexity, maintainability, anti-patterns, and API surface area.

**View the dashboard:** https://dbuytaert.github.io/drupal-core-metrics/


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

**Prerequisites:** PHP 8.1+, Python 3, Composer

### Regenerating data

```bash
composer install              # Install dependencies
python3 scripts/analyze.py    # Run analysis (15-30 min)
```

This generates `data.json`. The `index.html` file is static and does not need to be regenerated.

### Viewing the dashboard

The dashboard loads data via `fetch()`, which requires an HTTP server (browsers block this for local files). Start a simple server:

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000 in your browser.


## Contributing

Questions or ideas? Open an issue or PR.
