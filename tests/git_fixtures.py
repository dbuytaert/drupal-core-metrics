"""Real git repositories for tests: git behavior is tested against git, not faked output."""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false")


def run_git(repository: Path, *arguments: str, date: Optional[str] = None) -> str:
    """git's output in a repository, failing the test when git fails. A date (YYYY-MM-DD)
    sets the author and committer time to noon that day."""
    environment = {**os.environ, **({"GIT_AUTHOR_DATE": f"{date}T12:00:00", "GIT_COMMITTER_DATE": f"{date}T12:00:00"}
                                    if date else {})}
    return subprocess.run(["git", *IDENTITY, *arguments], cwd=repository, env=environment,
                          capture_output=True, text=True, check=True).stdout


def init(repository: Path) -> Path:
    repository.mkdir(parents=True, exist_ok=True)
    run_git(repository, "init", "--quiet", "--initial-branch", "main")
    return repository


def commit(repository: Path, message: str, date: Optional[str] = None) -> str:
    """Commit everything in the working tree, even nothing, and return the commit's hash."""
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "--quiet", "--allow-empty", "--message", message, date=date)
    return run_git(repository, "rev-parse", "HEAD").strip()


def commit_tree(repository: Path, files: dict[str, str], symlinks: Optional[dict[str, str]] = None) -> str:
    """Make {path: content} and {path: target} symbolic links the whole tree of a new commit."""
    if not (repository / ".git").exists():
        init(repository)
    for entry in repository.iterdir():
        if entry.name != ".git":
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for relative_path, target in (symlinks or {}).items():
        (repository / relative_path).symlink_to(target)
    return commit(repository, "Tree")


def commit_change(repository: Path, subject: str, date: str) -> str:
    """Commit a new file on the current branch, with the given subject and date."""
    Path(repository, f"change-{abs(hash((subject, date)))}.txt").write_text(subject)
    return commit(repository, subject, date)


def merge(repository: Path, branch: str, message: str, date: str) -> None:
    """Merge a branch into the current one without fast-forwarding, as of the given date."""
    run_git(repository, "merge", "--quiet", "--no-ff", "--message", message, branch, date=date)


def git_history(directory: Path, commits: list[tuple[str, str, str]]) -> Path:
    """A real repository built from (branch, date, subject) commits, in order. A branch that
    does not exist yet starts from the first commit."""
    init(directory)
    first = None
    for branch, date, subject in commits:
        if first is not None:
            exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=directory,
                                    capture_output=True).returncode == 0
            run_git(directory, "checkout", "--quiet", *([branch] if exists else ["-b", branch, first]))
        head = commit_change(directory, subject, date)
        first = first or head
    return directory
