"""The facts extract-facts.php measures in the files of a repository's snapshots, held in memory
for one run.

A snapshot is its tree: the path of every file and the facts of its content. A file's facts depend
on its content alone, so each content is measured once however many snapshots and paths hold it,
and every entry holding it shares one object. The extractor refers to declarations by number;
each reference is resolved into the declaration it names, and facts compare by identity, so a rule
can never pair one file's facts with another's. Symbolic links and git submodules are not files.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EXTRACTOR = Path(__file__).parent / "extract-facts.php"

# Enough of a file's start to hold a shebang line and the opening tag that follows it.
OPENING_BYTES = 200

# Facts are immutable and compare by identity: two files with equal facts stay two files.
fact = dataclass(frozen=True, slots=True, eq=False)


class FactError(RuntimeError):
    """Git or the extractor failed, so the facts cannot answer for a snapshot."""


@fact
class Argument:
    name: Optional[str]
    value: Optional[str]


@fact
class Attribute:
    name: str
    arguments: tuple[Argument, ...]


@fact
class Declaration:
    parent: Optional[Declaration]
    kind: str
    name: Optional[str]
    resolved_name: Optional[str]
    doc: Optional[str]
    attributes: tuple[Attribute, ...]
    extends: tuple[str, ...]
    implements: tuple[str, ...]
    lcom4: Optional[int]
    has_body: Optional[bool]
    visibility: Optional[str]
    parameter_types: tuple[Optional[tuple[str, ...]], ...]
    return_type: Optional[tuple[str, ...]]
    cognitive: Optional[int]
    cyclomatic: Optional[int]


@fact
class StringKey:
    function: Optional[Declaration]
    syntax: str
    name: str
    line: int


@fact
class ServiceLocatorCall:
    function: Optional[Declaration]


@fact
class ArrayDepth:
    function: Optional[Declaration]
    depth: int
    occurrences: int


@fact
class SubscribedEventKey:
    function: Declaration
    name: str


@fact
class PhpContent:
    parse: str
    code_line_count: int
    class_like_line_count: int
    function_line_count: int
    code_line_runs: tuple[tuple[int, int], ...]
    opening_comments: str
    literal_only: Optional[bool]
    text_functions: tuple[str, ...]
    declarations: tuple[Declaration, ...]
    string_keys: tuple[StringKey, ...]
    service_locator_calls: tuple[ServiceLocatorCall, ...]
    array_depths: tuple[ArrayDepth, ...]
    subscribed_event_keys: tuple[SubscribedEventKey, ...]


@fact
class JavaScriptContent:
    code_line_count: int


@fact
class ServiceKey:
    name: str
    value: Optional[str]


@fact
class YamlContent:
    service_keys: tuple[ServiceKey, ...]
    deprecated_key_count: int


@fact
class ComposerLock:
    valid: bool
    packages: tuple[str, ...]
    development: Optional[tuple[str, ...]]


Content = PhpContent | JavaScriptContent | YamlContent | ComposerLock


@fact
class Entry:
    path: str
    content: Optional[Content]


def file_kind(path: str) -> Optional[str]:
    """What the extractor reads a file as, from its name, or None for a file it does not read. PHP
    files carry the extensions Drupal gives them, Drupal 7's .test files among them. TypeScript reads as JavaScript although Core has never shipped
    one of its own: the single .ts file in its history is a third-party test fixture, left out twice
    over, so that clause is forward-looking and untested on purpose."""
    name = path.rpartition("/")[2]
    if name == "composer.lock":
        return "composer-lock"
    if name.endswith((".js", ".ts")):
        return "js"
    if name.endswith(".yml"):
        return "yaml"
    if "." in name and name.rpartition(".")[2] in {"php", "module", "inc", "install", "theme", "profile", "engine", "test"}:
        return "php"
    return None


def is_php_script(start: bytes) -> bool:
    """Whether a content is PHP, read from its first bytes: it opens with <?php, or with a #! line
    naming php. Core's command-line scripts are PHP whatever they are named, run-tests.sh and
    password-hash.sh among them."""
    return start.startswith(b"<?php") or (start.startswith(b"#!") and b"php" in start.split(b"\n", 1)[0])


def php_scripts(repository: Path, blobs: list[str]) -> set[str]:
    """The contents among these that PHP runs. A file whose name says nothing about what it holds is
    read from the start, because an extension is a convention a file can simply not follow."""
    if not blobs:
        return set()
    scripts = set()
    with tempfile.TemporaryFile() as names:
        names.write("".join(f"{blob}\n" for blob in blobs).encode())
        names.seek(0)
        blob_contents = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repository,
                                         stdin=names, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for blob in blobs:
            header = blob_contents.stdout.readline().split()
            if len(header) != 3:
                raise FactError(f"git cat-file --batch did not return the content of {blob}")
            # The content, then the newline git writes after it, so the next header lines up.
            start = blob_contents.stdout.read(int(header[2]))[:OPENING_BYTES]
            blob_contents.stdout.read(1)
            if is_php_script(start):
                scripts.add(blob)
        blob_contents.stdout.close()
        blob_contents.wait()
    return scripts


def measure(repository: Path, commits: list[str]) -> dict[str, tuple[Entry, ...]]:
    """Each commit's tree, every file holding the facts of its content."""
    trees = {commit: tree(repository, commit) for commit in dict.fromkeys(commits)}
    unnamed = sorted({blob for entries in trees.values() for path, blob in entries if file_kind(path) is None})
    scripts = php_scripts(repository, unnamed)

    def kind(path: str, blob: str) -> Optional[str]:
        """What a file is read as: what its name says, or, where its name says nothing, its content."""
        return file_kind(path) or ("php" if blob in scripts else None)

    read_as = {commit: [(path, blob, kind(path, blob)) for path, blob in entries] for commit, entries in trees.items()}
    contents = extract_all(repository, {(blob, blob_kind) for entries in read_as.values()
                                        for _, blob, blob_kind in entries if blob_kind})
    return {commit: tuple(Entry(path, contents[blob, blob_kind] if blob_kind else None)
                          for path, blob, blob_kind in entries)
            for commit, entries in read_as.items()}


def tree(repository: Path, commit: str) -> list[tuple[str, str]]:
    """The path and content hash of every file in a commit's tree."""
    result = subprocess.run(["git", "ls-tree", "-r", "-z", commit], cwd=repository, capture_output=True)
    if result.returncode != 0:
        raise FactError(f"git ls-tree {commit} failed: {result.stderr.decode().strip()}")
    entries = []
    for record in result.stdout.split(b"\0"):
        if record:
            description, _, path = record.partition(b"\t")
            mode, _, blob = description.decode().split(" ")
            if mode in ("100644", "100755"):
                entries.append((path.decode(), blob))
    return entries


def extract_all(repository: Path, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], Content]:
    """The facts of each (content hash, kind), measured in parallel batches of up to 500 contents."""
    blobs_by_kind: dict[str, list[str]] = {}
    for blob, kind in sorted(wanted):
        blobs_by_kind.setdefault(kind, []).append(blob)
    batches = [(kind, blobs[start:start + 500]) for kind, blobs in blobs_by_kind.items()
               for start in range(0, len(blobs), 500)]
    contents = {}
    with ThreadPoolExecutor(os.cpu_count()) as pool:
        # A failed batch raises here, which stops the run.
        for (kind, _), batch in zip(batches, pool.map(lambda batch: extract(repository, *batch), batches)):
            for facts in batch:
                blob = facts.pop("blob")
                contents[blob, kind] = content(kind, facts)
    return contents


def extract(repository: Path, kind: str, blobs: list[str]) -> list[dict]:
    """The extractor's facts for a batch of contents of one kind, as parsed JSON."""
    # A content git cannot read reaches the extractor as "<hash> missing", which it rejects on stderr.
    with tempfile.TemporaryFile() as names:
        names.write("".join(f"{blob}\n" for blob in blobs).encode())
        names.seek(0)
        blob_contents = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repository,
                                         stdin=names, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        extractor = subprocess.Popen(["php", "-dshort_open_tag=1", "-ddisplay_errors=stderr", str(EXTRACTOR), kind],
                                     stdin=blob_contents.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        blob_contents.stdout.close()
        output, errors = extractor.communicate()
        blob_contents.wait()
        # A warning PHP reports before the extractor's own handler exists still means a fact may be wrong.
        if extractor.returncode != 0 or errors:
            raise FactError(f"extract-facts.php {kind} failed: {errors.decode().strip()[-2000:]}")
    return [json.loads(line) for line in output.splitlines()]


def content(kind: str, facts: dict) -> Content:
    """One content's facts as objects, every reference resolved."""
    try:
        if kind == "php":
            return php_content(facts)
        if kind == "js":
            return build(JavaScriptContent, facts)
        if kind == "yaml":
            return build(YamlContent, facts, service_keys=tuple(build(ServiceKey, key) for key in facts.pop("serviceKeys")))
        return build(ComposerLock, facts)
    except (TypeError, KeyError) as error:
        raise FactError(f"extract-facts.php {kind} facts do not match their fields: {error}") from error


def php_content(facts: dict) -> PhpContent:
    declarations: dict[int, Declaration] = {}
    for item in facts.pop("declarations"):
        attributes = tuple(build(Attribute, attribute, arguments=tuple(build(Argument, argument)
                                                                       for argument in attribute.pop("arguments")))
                           for attribute in item.pop("attributes"))
        number, parent = item.pop("id"), item.pop("parent")
        declarations[number] = build(Declaration, item, attributes=attributes,
                                     parent=None if parent is None else declarations[parent])

    def located(fact_class: type, items: list[dict]) -> tuple:
        """Items with the declaration they sit in resolved, None at file level."""
        located_items = []
        for item in items:
            function = item.pop("function")
            located_items.append(build(fact_class, item, function=None if function is None else declarations[function]))
        return tuple(located_items)

    return build(PhpContent, facts, declarations=tuple(declarations.values()),
                 string_keys=located(StringKey, facts.pop("stringKeys")),
                 service_locator_calls=located(ServiceLocatorCall, facts.pop("serviceLocatorCalls")),
                 array_depths=located(ArrayDepth, facts.pop("arrayDepths")),
                 subscribed_event_keys=located(SubscribedEventKey, facts.pop("subscribedEventKeys")))


def build(fact_class: type, facts: dict, **resolved):
    """A fact built by keyword from the extractor's camelCase JSON, every array a tuple: a field the
    JSON lacks or a key the class lacks raises, so a changed extractor stops the run."""
    return fact_class(**{snake_case(key): as_tuples(value) for key, value in facts.items()}, **resolved)


def snake_case(name: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name).lower()


def as_tuples(value):
    return tuple(as_tuples(item) for item in value) if isinstance(value, list) else value
