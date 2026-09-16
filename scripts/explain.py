"""Lists the items behind a codebase number, and the items a rule change moves.

  python3 scripts/explain.py --definitions
      the definitions a snapshot answers
  python3 scripts/explain.py DEFINITION COMMIT
      what DEFINITION holds for the snapshot at COMMIT (a prefix of its hash is enough)
  python3 scripts/explain.py DEFINITION COMMIT --against OTHER.py
      what DEFINITION holds under OTHER.py, a changed copy of definitions.py, and not under
      definitions.py, and the reverse: every item a rule change would add to or drop from a number

Measures the snapshot from the Drupal Core clone analyze.py keeps in drupal-core/, or --repository.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import subprocess
import sys
from functools import cached_property
from pathlib import Path

import definitions
import facts

REPOSITORY = Path(__file__).parent.parent / "drupal-core"


def definition_names(module) -> list[str]:
    return sorted(name for name, value in vars(module.Snapshot).items() if isinstance(value, cached_property))


def load_definitions(path: Path):
    specification = importlib.util.spec_from_file_location("changed_definitions", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def describe(item) -> str:
    """An item as one line: a file by its path, a declaration by its kind and name, a record by its fields."""
    if isinstance(item, facts.Declaration):
        return f"{item.kind} {item.name or 'anonymous'}"
    # A file from definitions.py or from a changed copy of it, which defines a File class of its own.
    if hasattr(item, "prefix") and hasattr(item, "path"):
        return item.path
    if hasattr(item, "_fields"):
        return "  ".join(f"{field}={describe(value)}" for field, value in zip(item._fields, item))
    if isinstance(item, tuple):
        return "  ".join(describe(value) for value in item)
    if isinstance(item, dict):
        return "  ".join(f"{key}={value}" for key, value in item.items())
    if hasattr(item, "__slots__"):
        return "  ".join(f"{field}={describe(getattr(item, field))}" for field in item.__slots__)
    return str(item)


def items(module, entries: tuple[facts.Entry, ...], definition: str) -> collections.Counter:
    if definition not in definition_names(module):
        sys.exit(f"No definition is named {definition!r}; --definitions lists them.")
    answer = getattr(module.Snapshot(entries), definition)
    if isinstance(answer, dict):
        answer = answer.items()
    elif not isinstance(answer, (list, set)):
        answer = [answer]
    return collections.Counter(map(describe, answer))


def print_items(counted: collections.Counter, label: str = "") -> None:
    for item, times in sorted(counted.items()):
        print(f"{label}{item}" + (f"  (x{times})" if times > 1 else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("definition", nargs="?")
    parser.add_argument("commit", nargs="?")
    parser.add_argument("--against", type=Path, help="a changed copy of definitions.py to compare with")
    parser.add_argument("--repository", type=Path, default=REPOSITORY)
    parser.add_argument("--definitions", action="store_true", help="list the definitions")
    arguments = parser.parse_args()
    if arguments.definitions:
        print("\n".join(definition_names(definitions)))
        return
    if not (arguments.definition and arguments.commit):
        parser.error("give a DEFINITION and a COMMIT, or --definitions")

    resolved = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{arguments.commit}^{{commit}}"],
                              cwd=arguments.repository, capture_output=True, text=True)
    if resolved.returncode != 0:
        sys.exit(f"No commit {arguments.commit!r} in {arguments.repository}")
    commit = resolved.stdout.strip()
    entries = facts.measure(arguments.repository, [commit])[commit]
    current = items(definitions, entries, arguments.definition)
    if arguments.against is None:
        print_items(current)
        print(f"{sum(current.values())} items")
        return

    changed = items(load_definitions(arguments.against), entries, arguments.definition)
    added, dropped = changed - current, current - changed
    print_items(added, "+ ")
    print_items(dropped, "- ")
    print(f"{sum(added.values())} items added and {sum(dropped.values())} dropped by {arguments.against}")


if __name__ == "__main__":
    main()
