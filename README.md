# Drupal Core Metrics

A dashboard that tracks Drupal core's codebase over time: lines of code, complexity, code composition, class cohesion, deprecated APIs, anti-patterns, and API surface area.

**View the dashboard:** https://dbuytaert.github.io/drupal-core-metrics/

**Learn more:** [Measuring Drupal Core code complexity](https://dri.es/measuring-drupal-core-code-complexity)


## Metrics

### Code quality
- **SLOC**: Source lines of code (excluding blanks and comments)
- **Complexity**: measured with [cognitive complexity](https://www.sonarsource.com/resources/cognitive-complexity/) (SonarSource) — how hard code is to *read*, weighting deep nesting heavily and forgiving shorthand that reads linearly (a flat `switch`, a `??` chain). It drives the complexity trend chart and ranks the hotspots. Cyclomatic complexity is kept only as a contrast column in the hotspots, not as a standalone metric, because at the codebase aggregate the two are near-identical.
- **Code composition**: Share of production lines that are object-oriented PHP, procedural PHP, and JavaScript/TypeScript.
- **Class cohesion (LCOM4)**: Whether a class's methods form one connected unit or several unrelated groups. Lower is more focused.
- **Deprecated API surface**: Count of code, services and libraries marked deprecated and awaiting removal at the next major version.
- **API type coverage**: Share of public method parameters and return values that carry an explicit type, a proxy for how readily an IDE or AI assistant can build against the API without reading the implementation. Starts at the Drupal 8.0 release, when the object-oriented API and PHP 7 return types arrived.
- **Complexity hotspots**: The classes and functions hardest to read, ranked by cognitive complexity as concrete refactoring targets and linked to the code, with cyclomatic complexity shown alongside for contrast.
- **Third-party dependencies**: Composer packages core requires, direct and transitive; Drupal's own packages excluded.

### Anti-patterns
Code patterns with known downsides. Tracked as absolute counts and as density per 1k lines.

| Pattern | Description |
|---------|-------------|
| Magic keys | `#`-prefixed array keys naming a render-array property (`#theme`, `#access`). Inherent to Drupal's render array architecture. |
| Deep arrays | 3+ levels of nesting. Hard to read and refactor. |
| Service locators | Static `\Drupal::` calls and `$this->container->get()`. Hide dependencies, hinder testing. |

### API surface area
Distinct extension points in Drupal. A larger surface may correlate with a steeper learning curve.

| Category | Examples |
|----------|----------|
| Global functions | t(), drupal_static() |
| Hooks | hook_form_alter, hook_entity_presave |
| Services | cache, entity, router |
| Events | KernelEvents::REQUEST |
| Plugin types | Block, Field, ViewsDisplay |
| YAML formats | routing, permissions, services |
| Magic keys | #theme, #states, #ajax (vocabulary size) |
| Interface methods | EntityInterface::save, CacheBackendInterface::get |

### Community
- **Contributors**: Distinct credited people per year, split into first-time and returning. Parsed from the credit lists in commit messages on every branch, counting a person once per issue as drupal.org's credit system does; coverage begins in 2010.
- **Credits by generation**: Each year's credits grouped by the year each person was first credited, with a table naming newer contributors among the year's most-credited.
- **People with half the credits**: How few people account for half of a year's credits.


## Running locally

**Prerequisites:** PHP 8.1+, Python 3.10+, Composer, git, and for the tests Node.js and Chrome or Chromium.

### Regenerating data

```bash
composer install              # Install PHP dependencies (nikic/php-parser)
python3 scripts/analyze.py    # Run analysis (15-30 min)
```

This generates `data.json`. A local run is for testing and checking the diff; only the CI workflow commits `data.json`. The `index.html` file is static and does not need to be regenerated.

`python3 -m unittest discover tests` runs the behavior tests for the codebase definitions
and analyze.py, the git history walks (against real repositories) and the dashboard's chart
rules, and renders the dashboard in a headless browser; CI runs them after each analysis, before
publishing.

`python3 scripts/explain.py DEFINITION COMMIT` lists the files, functions or keys behind a
codebase number at any commit of the `drupal-core/` clone a run leaves; `--definitions`
lists the definitions, and `--against changed.py` lists what a changed copy of
`scripts/definitions.py` would add or drop.

### Viewing the dashboard

The dashboard loads data via `fetch()`, which requires an HTTP server (browsers block this for local files). Start a simple server:

```bash
python3 -m http.server 8000
```

Then open http://localhost:8000 in your browser.


## Contributing

Questions or ideas? Open an issue or PR.
