"""Core git archaeology logic for mcp-git-historian.

Pure Python stdlib: every function shells out to the local ``git`` CLI via
``subprocess`` and parses machine-friendly output, namely custom ``--pretty``
formats with ASCII unit/record separators (``%x1f`` / ``%x1e``), NUL-terminated
``--numstat -z`` / ``--name-status -z`` records and ``git blame --incremental``.
No third-party dependencies, no network access.

Conventions shared by every tool:

* ``repo_path`` may be the repository root **or any directory inside it**. The
  real root is resolved with ``git rev-parse --show-toplevel`` and, when a
  sub-directory was given, results are scoped to it (``scope`` in the output).
  File paths in results are always relative to the repository root.
* Author identities go through ``.mailmap`` (``%aN``), exactly like
  ``git blame`` does, so one person with two e-mail addresses is one author.
* Merge commits are excluded from authorship/churn statistics by default:
  merging a pull request is not writing its code.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Iterator, Optional, Tuple

GIT_TIMEOUT = 30  # seconds; override with the GIT_HISTORIAN_TIMEOUT env var
TIMEOUT_ENV = "GIT_HISTORIAN_TIMEOUT"
FIELD_SEP = "\x1f"  # ASCII unit separator: between fields of one commit
RECORD_SEP = "\x1e"  # ASCII record separator: between commits

# Record-per-commit format: short hash, author date, mailmapped author, subject.
_COMMIT_FMT = f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%as{FIELD_SEP}%aN{FIELD_SEP}%s"

_HINT = "high churn — candidate for refactoring or extra review"
HOTSPOT_MIN_COMMITS = 3  # a file needs at least this many commits to be flagged...
HOTSPOT_PERCENTILE = 90.0  # ...and must sit in the top decile of its window
SILO_PERCENT = 80.0  # one author above this share of a directory's commits = silo...
SILO_MIN_COMMITS = 3  # ...provided the directory has at least this many commits
ORPHAN_PERCENT = 50.0  # inactive authors own more than this share of a file = orphaned
MAX_COMMIT_FILES_LISTED = 300  # commit_details lists at most this many files
MAX_REFS_LISTED = 50  # commit_details lists at most this many branches/tags

# Config overrides so a user's ~/.gitconfig cannot break parsing
# (signatures injected into log output, colours, non-UTF-8 log encoding,
# rename detection switched off, quoted non-ASCII paths).
_GIT_CONFIG = (
    "-c", "core.quotepath=off",
    "-c", "log.showSignature=false",
    "-c", "color.ui=never",
    "-c", "i18n.logOutputEncoding=UTF-8",
    "-c", "diff.renames=true",
)

# A change inside one commit: (old_path, new_path, lines_added, lines_deleted).
# old_path == new_path unless the file was renamed/copied; the line counts
# are None for binary files.
Change = Tuple[str, str, Optional[int], Optional[int]]


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def git_timeout() -> float:
    """Seconds each git call may run: ``$GIT_HISTORIAN_TIMEOUT`` or 30."""
    raw = os.environ.get(TIMEOUT_ENV, "").strip()
    if not raw:
        return float(GIT_TIMEOUT)
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{TIMEOUT_ENV}={raw!r} is not a positive number of seconds — unset it "
            f"or set it to something like {TIMEOUT_ENV}=120."
        )
    return value


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    # Untranslated error messages (we match on them) and no optional locks:
    # every tool here is read-only.
    env["LC_ALL"] = "C"
    env["LANGUAGE"] = "C"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _exec_git(repo_path: str, args: tuple[str, ...]) -> subprocess.CompletedProcess:
    path = Path(repo_path).expanduser()
    if not path.exists():
        raise ValueError(
            f"Repo not found at {repo_path} — pass an absolute path to a local git repository."
        )
    if not path.is_dir():
        raise ValueError(
            f"{repo_path} is a file, not a directory — pass the folder of a git repository."
        )
    timeout = git_timeout()
    cmd = ["git", "-C", str(path), *_GIT_CONFIG, *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise ValueError(
            "git executable not found — install git and make sure it is on your PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"'git {args[0]}' timed out after {timeout:g}s on {repo_path} — the repository "
            "may be very large or on a slow disk; narrow the query (e.g. a shorter 'since') "
            f"or raise the limit with the {TIMEOUT_ENV} environment variable."
        ) from None


def run_git(repo_path: str, *args: str) -> str:
    """Run ``git -C <repo_path> <args>`` and return stdout as text.

    Raises ValueError with an actionable message when git is not installed,
    the path does not exist, the path is not a git repository, the repo has
    no commits yet, or the command fails/times out. Output is decoded as
    UTF-8 with ``errors="replace"`` so odd bytes never crash a tool.
    """
    result = _exec_git(repo_path, args)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        low = stderr.lower()
        if "not a git repository" in low:
            raise ValueError(
                f"{repo_path} is not a git repository — pass the root folder of a "
                "repo (the one containing .git) or any folder inside it."
            )
        if "does not have any commits" in low or (
            "ambiguous argument 'head'" in low and "unknown revision" in low
        ):
            raise ValueError(
                f"The repository at {repo_path} has no commits yet — nothing to analyze."
            )
        raise ValueError(f"'git {args[0]}' failed: {stderr or 'unknown error'}")
    return result.stdout


def _try_git(repo_path: str, *args: str) -> str | None:
    """Like run_git, but return None instead of raising when git exits non-zero."""
    result = _exec_git(repo_path, args)
    return result.stdout if result.returncode == 0 else None


@dataclass(frozen=True)
class Repo:
    """A resolved repository: what the caller passed, the real root, the scope."""

    path: str  # as given by the caller (echoed back in results)
    root: str  # absolute work-tree root, as printed by git
    prefix: str  # "" or the sub-directory the caller pointed at, e.g. "src/"

    @property
    def scope(self) -> str | None:
        return self.prefix or None

    def in_scope(self, file: str) -> bool:
        return file.startswith(self.prefix)


def open_repo(repo_path: str) -> Repo:
    """Resolve repo_path (root or any folder inside a work tree) to a Repo.

    Raises ValueError for missing paths, non-repositories, bare repositories
    and repositories without commits.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError(
            "repo_path must be a non-empty string — pass an absolute path to a local "
            "git repository."
        )
    inside = run_git(repo_path, "rev-parse", "--is-inside-work-tree").strip()
    if inside != "true":
        raise ValueError(
            f"{repo_path} is not inside a git work tree — bare repositories and .git "
            "folders are not supported; pass a normal checkout."
        )
    lines = run_git(repo_path, "rev-parse", "--show-toplevel", "--show-prefix").split("\n")
    root = lines[0].strip()
    prefix = lines[1].strip() if len(lines) > 1 else ""
    if _try_git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is None:
        raise ValueError(
            f"The repository at {repo_path} has no commits yet — nothing to analyze."
        )
    return Repo(path=repo_path, root=root, prefix=prefix)


def _ensure_repo(repo_path: str) -> None:
    """Backward-compatible validator (kept for callers of 0.1.x)."""
    open_repo(repo_path)


def _base(repo: Repo) -> dict:
    return {"repo_path": repo.path, "repo_root": repo.root, "scope": repo.scope}


def _positive_int(name: str, value: object, example: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer — e.g. {name}={example} (got {value!r}).")
    if value < 1:
        raise ValueError(
            f"{name} must be at least 1 (got {value}) — e.g. {name}={example}. "
            "Zero or negative values are not a shortcut for 'unlimited'."
        )
    return value


def _day(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _resolve_date(repo: Repo, value: str, name: str) -> int:
    """Turn a git date expression into a Unix timestamp using git's own parser.

    git's approxidate silently turns nonsense into "now", which would make a
    query match nothing; that case is reported as an error instead.
    """
    out = run_git(repo.root, "rev-parse", f"--since={value}").strip()
    if not out.startswith("--max-age="):
        raise ValueError(f"git could not parse {name}={value!r}.")
    epoch = int(out.split("=", 1)[1])
    text = value.strip().lower()
    if abs(epoch - time.time()) <= 5 and text not in ("now", "today") and not text[:1].isdigit():
        raise ValueError(
            f"git could not understand {name}={value!r} (it resolves to 'now', so nothing "
            "would match) — use forms like '6 months ago', '2 weeks ago' or '2025-01-01'."
        )
    return epoch


def _since_args(repo: Repo, since: str) -> tuple[list[str], str | None]:
    if not since:
        return [], None
    epoch = _resolve_date(repo, since, "since")
    return [f"--since={since}"], _day(epoch)


def _repo_file(repo: Repo, file: str, *, allow_dir: bool = False) -> str:
    """Normalise a user-supplied path to a repo-root-relative POSIX path.

    Accepts root-relative paths, paths relative to the sub-directory the
    caller pointed at, and absolute paths inside the work tree.
    """
    if not isinstance(file, str) or not file.strip():
        what = "path" if allow_dir else "file"
        raise ValueError(
            f"{what} must be a non-empty path relative to the repository root "
            "(e.g. 'src/app.py')."
        )
    raw = file.strip()
    root = Path(repo.root)
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            raise ValueError(
                f"{file} is outside the repository at {repo.root} — pass a path "
                "inside it, relative to the repository root."
            ) from None
        return rel.as_posix() if rel.parts else ""
    norm = raw.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.rstrip("/")
    if repo.prefix and not norm.startswith(repo.prefix):
        if not (root / norm).exists() and (root / repo.prefix / norm).exists():
            return repo.prefix + norm
    return norm


def _head_files(repo: Repo) -> set[str]:
    """Every file path tracked at HEAD (root-relative)."""
    out = run_git(repo.root, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    return {p for p in out.split("\0") if p}


def _parse_commit_records(out: str) -> list[dict]:
    """Parse output produced with _COMMIT_FMT into a list of commit dicts."""
    commits = []
    for chunk in out.split(RECORD_SEP):
        chunk = chunk.strip("\n\0")
        if not chunk:
            continue
        parts = chunk.split(FIELD_SEP)
        if len(parts) < 4:
            continue
        commits.append(
            {"hash": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]}
        )
    return commits


def _parse_numstat_z(body: str) -> list[Change]:
    """Parse NUL-terminated ``--numstat -z`` records (a rename spans 3 tokens)."""
    tokens = body.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i].lstrip("\n")
        i += 1
        if not tok:
            continue
        parts = tok.split("\t")
        if len(parts) != 3:
            continue
        add, dele, path = parts
        if path == "":  # rename/copy: "<add>\t<del>\t\0<old>\0<new>\0"
            if i + 1 >= len(tokens):
                break
            old, new = tokens[i], tokens[i + 1]
            i += 2
        else:
            old = new = path
        changes.append(
            (old, new, None if add == "-" else int(add), None if dele == "-" else int(dele))
        )
    return changes


def _log_changes(repo: Repo, *extra: str, fields: str = "%H") -> Iterator[tuple[list[str], list[Change]]]:
    """Yield (header_fields, changes) per commit, newest first.

    Uses ``git log -z -M --numstat`` so paths with spaces, tabs or " => " in
    their names are parsed exactly and renames are always detected.
    """
    out = run_git(
        repo.root, "log", "-z", "-M", "--numstat", f"--pretty=format:{RECORD_SEP}{fields}", *extra
    )
    for chunk in out.split(RECORD_SEP):
        if not chunk.strip("\0\n"):
            continue
        header, _, body = chunk.partition("\n")
        yield header.strip("\0").split(FIELD_SEP), _parse_numstat_z(body)


class _RenameTracker:
    """Map historical paths to their newest name while walking newest → oldest.

    When a commit renames ``old`` → ``new``, every older commit that touched
    ``old`` belongs to the history of whatever ``new`` is called today.
    """

    def __init__(self) -> None:
        self.alias: dict[str, str] = {}
        self.former: dict[str, set[str]] = defaultdict(set)

    def resolve(self, old: str, new: str) -> str:
        current = self.alias.get(new, new)
        if old != new:
            self.alias[old] = current
            self.former[current].add(old)
        return current


def _percentile_ranks(counts: dict[str, int]) -> dict[str, float]:
    """Mid-rank percentile of each value: 100 * (below + equal/2) / n.

    Ties share a rank, so when every file has the same number of commits they
    all sit at 50: nothing stands out, nothing gets flagged.
    """
    values = sorted(counts.values())
    n = len(values)
    ranks = {}
    for key, value in counts.items():
        below = bisect_left(values, value)
        equal = bisect_right(values, value) - below
        ranks[key] = round(100 * (below + equal / 2) / n, 1)
    return ranks


def _last_n_months(end_month: str, n: int = 12) -> list[str]:
    """Return the n calendar months (YYYY-MM) ending at end_month, oldest first."""
    year, month = (int(p) for p in end_month.split("-"))
    months = []
    for _ in range(n):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))


def _blame(repo: Repo, file: str) -> tuple[Counter, tuple[int, str] | None, tuple[int, str] | None]:
    """Line ownership of ``file`` at HEAD via ``git blame --incremental``.

    Returns (lines per mailmapped author, (oldest_epoch, author), (newest_epoch, author)).
    """
    out = run_git(repo.root, "blame", "--incremental", "HEAD", "--", file)
    commit_info: dict[str, dict] = {}
    lines: Counter = Counter()
    oldest: tuple[int, str] | None = None
    newest: tuple[int, str] | None = None
    sha = None
    count = 0
    for line in out.splitlines():
        if sha is None:
            parts = line.split(" ")
            if len(parts) == 4 and len(parts[0]) in (40, 64):
                sha, count = parts[0], int(parts[3])
                commit_info.setdefault(sha, {})
            continue
        if line.startswith("author "):
            commit_info[sha]["author"] = line[len("author "):]
        elif line.startswith("author-time "):
            commit_info[sha]["time"] = int(line.split()[1])
        elif line.startswith("filename "):
            info = commit_info[sha]
            author = info.get("author", "(unknown)")
            stamp = info.get("time", 0)
            lines[author] += count
            if oldest is None or stamp < oldest[0]:
                oldest = (stamp, author)
            if newest is None or stamp > newest[0]:
                newest = (stamp, author)
            sha = None
    return lines, oldest, newest


def _merge_count(repo: Repo) -> int:
    args = ["rev-list", "--count", "--merges", "HEAD"]
    if repo.prefix:
        args += ["--", repo.prefix]
    return int(run_git(repo.root, *args).strip() or 0)


def _dir_key(prefix: str, path: str) -> str:
    """Group a path by the first directory below ``prefix`` ("(root)" at the top)."""
    rest = path[len(prefix):] if path.startswith(prefix) else path
    if "/" in rest:
        return prefix + rest.split("/", 1)[0]
    return prefix.rstrip("/") or "(root)"


def _by_count_then_name(items) -> list:
    return sorted(items, key=lambda kv: (-kv[1], kv[0]))


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def repo_summary(repo_path: str, include_merges: bool = False) -> dict:
    """Overview of a repository: current branch, total and merge commits,
    first/last commit (date + short hash), top 10 contributors by authored
    (non-merge) commits and commit counts per month for the 12 calendar
    months ending at the most recent commit. Pointing ``repo_path`` at a
    sub-directory summarises only the history of that directory."""
    repo = open_repo(repo_path)
    branch = run_git(repo.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    args = ["log", f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%aN{FIELD_SEP}%as{FIELD_SEP}%P"]
    if repo.prefix:
        args += ["--", repo.prefix]
    records = []
    for chunk in run_git(repo.root, *args).split(RECORD_SEP):
        # not .strip(): Python counts the unit separator as whitespace and
        # would eat the empty parent field of the root commit
        chunk = chunk.strip("\n\0")
        if chunk:
            parts = chunk.split(FIELD_SEP)
            if len(parts) >= 4:
                records.append(parts)
    if not records:
        raise ValueError(
            f"No commits found under {repo.prefix or repo.root} — nothing to analyze."
        )

    newest, oldest = records[0], records[-1]
    merges = [r for r in records if len(r[3].split()) > 1]
    counted = records if include_merges else [r for r in records if len(r[3].split()) <= 1]
    author_counts = Counter(r[1] for r in counted)
    month_counts = Counter(r[2][:7] for r in counted)
    months = _last_n_months(newest[2][:7], 12)

    return {
        **_base(repo),
        "branch": branch,
        "total_commits": len(records),
        "merge_commits": len(merges),
        "include_merges": include_merges,
        "first_commit": {"hash": oldest[0], "date": oldest[2]},
        "last_commit": {"hash": newest[0], "date": newest[2]},
        "total_authors": len(author_counts),
        "top_contributors": [
            {"author": author, "commits": count}
            for author, count in _by_count_then_name(author_counts.items())[:10]
        ],
        "activity_by_month": {m: month_counts.get(m, 0) for m in months},
        "note": (
            "Authors are mailmap-resolved. Contributor and monthly counts "
            + ("include" if include_merges else "exclude")
            + " merge commits."
        ),
    }


def hotspots(repo_path: str, since: str = "1 year ago", top: int = 15) -> dict:
    """Files ranked by change frequency (number of commits touching them)
    plus aggregated lines added/deleted. Rename-aware: commits made under a
    file's former names count towards its current name. Files no longer
    tracked at HEAD are excluded and merge commits are ignored (their changes
    were already counted in the commits they merge). A file is flagged as a
    high-churn hotspot only when it has at least 3 commits **and** sits in
    the top decile of the commit-count distribution (``churn_percentile``)."""
    repo = open_repo(repo_path)
    top = _positive_int("top", top, 15)
    since_args, since_date = _since_args(repo, since)
    tracked = _head_files(repo)

    tracker = _RenameTracker()
    commit_counts: Counter = Counter()
    added: Counter = Counter()
    deleted: Counter = Counter()
    for _header, changes in _log_changes(repo, "--no-merges", *since_args):
        seen: set[str] = set()
        for old, new, add, dele in changes:
            current = tracker.resolve(old, new)
            if current not in seen:
                commit_counts[current] += 1
                seen.add(current)
            added[current] += add or 0
            deleted[current] += dele or 0

    live = {f: c for f, c in commit_counts.items() if f in tracked and repo.in_scope(f)}
    ranks = _percentile_ranks(live) if live else {}
    ranked = sorted(live, key=lambda f: (-live[f], -(added[f] + deleted[f]), f))
    result = []
    for f in ranked[:top]:
        entry = {
            "file": f,
            "commits": live[f],
            "lines_added": added[f],
            "lines_deleted": deleted[f],
            "churn_percentile": ranks[f],
        }
        former = sorted(tracker.former.get(f, ()))
        if former:
            entry["renamed_from"] = former
        if live[f] >= HOTSPOT_MIN_COMMITS and ranks[f] >= HOTSPOT_PERCENTILE:
            entry["hint"] = _HINT
        result.append(entry)

    return {
        **_base(repo),
        "since": since or "all history",
        "since_date": since_date,
        "files_changed": len(live),
        "hotspots": result,
        "hint_rule": (
            f"flagged when a file has >= {HOTSPOT_MIN_COMMITS} commits and churn_percentile "
            f">= {HOTSPOT_PERCENTILE:g} (top decile of the {len(live)} files changed in the window)"
        ),
    }


def file_history(repo_path: str, file: str, limit: int = 20) -> dict:
    """Commits that touched one file (newest first): short hash, date, author,
    subject and lines added/deleted per commit. Uses ``--follow`` so history
    is tracked across renames; rename commits carry a ``renamed_from`` key."""
    repo = open_repo(repo_path)
    limit = _positive_int("limit", limit, 20)
    path = _repo_file(repo, file)
    out = run_git(
        repo.root,
        "log",
        "--follow",
        "-z",
        "-M",
        "-n",
        str(limit),
        "--numstat",
        f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%as{FIELD_SEP}%aN{FIELD_SEP}%s",
        "--",
        path,
    )
    commits = []
    for chunk in out.split(RECORD_SEP):
        if not chunk.strip("\0\n"):
            continue
        header, _, body = chunk.partition("\n")
        parts = header.strip("\0").split(FIELD_SEP)
        if len(parts) < 4:
            continue
        entry = {
            "hash": parts[0],
            "date": parts[1],
            "author": parts[2],
            "subject": parts[3],
            "lines_added": 0,
            "lines_deleted": 0,
        }
        for old, new, add, dele in _parse_numstat_z(body):
            entry["lines_added"] = add or 0
            entry["lines_deleted"] = dele or 0
            if old != new:
                entry["renamed_from"] = old
        commits.append(entry)

    if not commits:
        raise ValueError(
            f"No commits found for '{file}' in {repo.root} — the path must be relative "
            "to the repository root (e.g. 'src/app.py') or absolute inside the repository."
        )
    return {**_base(repo), "file": path, "count": len(commits), "commits": commits}


def blame_summary(repo_path: str, file: str) -> dict:
    """Line ownership of a file at HEAD via ``git blame``: percentage of lines
    per (mailmapped) author, the dominant author, and the dates of the oldest
    and newest surviving lines."""
    repo = open_repo(repo_path)
    path = _repo_file(repo, file)
    line_counts, oldest, newest = _blame(repo, path)

    total = sum(line_counts.values())
    if total == 0:
        return {
            **_base(repo),
            "file": path,
            "total_lines": 0,
            "authors": [],
            "dominant_author": None,
            "oldest_line": None,
            "newest_line": None,
        }

    authors = [
        {"author": author, "lines": count, "percent": round(count * 100 / total, 1)}
        for author, count in _by_count_then_name(line_counts.items())
    ]
    return {
        **_base(repo),
        "file": path,
        "total_lines": total,
        "authors": authors,
        "dominant_author": authors[0]["author"],
        "oldest_line": {"date": _day(oldest[0]), "author": oldest[1]},
        "newest_line": {"date": _day(newest[0]), "author": newest[1]},
    }


def bus_factor(repo_path: str, top: int = 10, include_merges: bool = False) -> dict:
    """Knowledge-concentration analysis. Per top-level directory (or per
    sub-directory of the scope): the dominant author and their share of
    commits touching it; directories with at least 3 commits where one author
    owns >80% of them are flagged as knowledge silos. The global bus factor is the minimum
    number of authors whose commits together cover more than 50% of all
    commits (lower = riskier). Authors are mailmap-resolved and merge commits
    are excluded unless ``include_merges`` is true."""
    repo = open_repo(repo_path)
    top = _positive_int("top", top, 10)
    extra = [] if include_merges else ["--no-merges"]
    author_commits: Counter = Counter()
    dir_authors: dict[str, Counter] = {}
    for header, changes in _log_changes(repo, *extra, fields=f"%H{FIELD_SEP}%aN"):
        if len(header) < 2:
            continue
        author = header[1]
        dirs = {_dir_key(repo.prefix, new) for _old, new, _a, _d in changes if repo.in_scope(new)}
        if repo.prefix and not dirs:
            continue  # this commit did not touch the scoped directory
        author_commits[author] += 1
        for d in dirs:
            dir_authors.setdefault(d, Counter())[author] += 1

    total = sum(author_commits.values())
    if total == 0:
        raise ValueError(
            f"No commits found under {repo.prefix or repo.root} — nothing to analyze."
        )

    ranked_authors = _by_count_then_name(author_commits.items())
    covered = 0
    factor = 0
    for _author, count in ranked_authors:
        covered += count
        factor += 1
        if covered * 2 > total:  # strictly more than 50%
            break

    directories = []
    silos = []
    for d, counter in sorted(dir_authors.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        dir_total = sum(counter.values())
        dom_author, dom_count = _by_count_then_name(counter.items())[0]
        dom_percent = round(dom_count * 100 / dir_total, 1)
        # A directory touched once by one person is not a silo, just quiet.
        is_silo = dom_percent > SILO_PERCENT and dir_total >= SILO_MIN_COMMITS
        if is_silo:
            silos.append(d)
        directories.append(
            {
                "directory": d,
                "commits": dir_total,
                "authors": len(counter),
                "dominant_author": dom_author,
                "dominant_percent": dom_percent,
                "knowledge_silo": is_silo,
            }
        )

    return {
        **_base(repo),
        "bus_factor": factor,
        "bus_factor_explanation": (
            "minimum number of authors whose commits together cover >50% of all "
            "commits — lower means knowledge is concentrated in fewer people"
        ),
        "total_commits": total,
        "total_authors": len(author_commits),
        "include_merges": include_merges,
        "merge_commits_excluded": 0 if include_merges else _merge_count(repo),
        "top_authors": [
            {"author": a, "commits": c, "percent": round(c * 100 / total, 1)}
            for a, c in ranked_authors[:top]
        ],
        "directories": directories[:top],
        "knowledge_silos": silos,
        "silo_rule": (
            f"a directory with >= {SILO_MIN_COMMITS} commits where one author made more "
            f"than {SILO_PERCENT:g}% of them"
        ),
    }


def _regex_error(pattern: str, exc: ValueError) -> ValueError:
    detail = str(exc).split("fatal:", 1)[-1].strip()
    return ValueError(
        f"Invalid regular expression {pattern!r}: {detail} — fix the pattern, or pass "
        "regex=false to search for the text literally."
    )


def search_commits(
    repo_path: str,
    query: str,
    author: str = "",
    since: str = "",
    limit: int = 20,
    regex: bool = False,
) -> dict:
    """Search commit messages (subject and body) case-insensitively,
    optionally filtered by author (substring of the mailmapped name or
    e-mail) and date. ``query`` is literal text by default, so brackets,
    dots and stars need no escaping; pass ``regex=True`` to use a POSIX
    extended regular expression instead. Returns matches newest first. When
    ``repo_path`` is a sub-directory, only commits touching it are searched."""
    repo = open_repo(repo_path)
    if not isinstance(query, str) or not query.strip():
        raise ValueError(
            "query must be a non-empty string — pass a word or phrase to search "
            "commit messages for."
        )
    limit = _positive_int("limit", limit, 20)
    since_args, since_date = _since_args(repo, since)
    args = [
        "log",
        "--use-mailmap",
        "-i",
        "--extended-regexp" if regex else "--fixed-strings",
        f"--grep={query}",
        "-n",
        str(limit),
        _COMMIT_FMT,
        *since_args,
    ]
    if author:
        args.append(f"--author={author}")
    if repo.prefix:
        args += ["--", repo.prefix]
    try:
        out = run_git(repo.root, *args)
    except ValueError as exc:
        if regex and "'git log' failed" in str(exc):
            raise _regex_error(query, exc) from None
        raise
    commits = _parse_commit_records(out)
    return {
        **_base(repo),
        "query": query,
        "mode": "regex (POSIX extended)" if regex else "literal text",
        "filters": {"author": author or None, "since": since or None, "since_date": since_date},
        "count": len(commits),
        "commits": commits,
    }


def find_change(
    repo_path: str, pattern: str, file: str = "", limit: int = 10, regex: bool = False
) -> dict:
    """Find the commits that introduced or removed a piece of code.

    Default: git's pickaxe (``git log -S<pattern>``), which returns commits
    where the *number of occurrences* of the literal pattern changed, i.e.
    the code was added or deleted, not merely moved. With ``regex=True`` it
    switches to ``git log -G<regex>``, which matches the regex against added
    and removed diff lines and therefore also flags commits that edited or
    moved matching lines. With ``file`` the search follows that file across
    renames. Answers "when did this code appear/disappear?"."""
    repo = open_repo(repo_path)
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(
            "pattern must be a non-empty string — pass the code snippet or "
            "identifier to search for (e.g. 'MAGIC_TOKEN')."
        )
    limit = _positive_int("limit", limit, 10)
    flag = f"-G{pattern}" if regex else f"-S{pattern}"
    args = ["log", flag, "-n", str(limit), _COMMIT_FMT]
    path = None
    if file:
        path = _repo_file(repo, file)
        args.extend(["--follow", "--", path])
    elif repo.prefix:
        args.extend(["--", repo.prefix])
    try:
        out = run_git(repo.root, *args)
    except ValueError as exc:
        if regex and "'git log' failed" in str(exc):
            raise _regex_error(pattern, exc) from None
        raise
    commits = _parse_commit_records(out)
    return {
        **_base(repo),
        "pattern": pattern,
        "mode": "-G regex (diff lines matching)" if regex else "-S pickaxe (occurrence count changed)",
        "file": path,
        "count": len(commits),
        "commits": commits,
        "note": (
            "Pickaxe (-S) matches commits where the occurrence count of the "
            "pattern changed (code added/removed). git's -G flag (regex=true) "
            "instead regex-matches diff lines, also catching edits and moves."
        ),
    }


def change_coupling(
    repo_path: str,
    file: str = "",
    since: str = "1 year ago",
    min_shared: int = 3,
    max_files_per_commit: int = 30,
    top: int = 20,
) -> dict:
    """Temporal (change) coupling: pairs of files that keep changing in the
    same commits, a hidden dependency that static analysis cannot see.

    For each pair: ``shared_commits``, each file's revisions in the window,
    ``degree`` = shared / min(revisions) (1.0 means every change to the
    rarer file also touched the other one) and ``jaccard`` = shared / union
    of the commits touching either file. Rename-aware, merge commits ignored,
    and commits touching more than ``max_files_per_commit`` files (mass
    reformatting, licence headers, vendoring) are skipped as noise. With
    ``file`` it returns the partners of that one file instead of all pairs."""
    repo = open_repo(repo_path)
    min_shared = _positive_int("min_shared", min_shared, 3)
    top = _positive_int("top", top, 20)
    max_files_per_commit = _positive_int("max_files_per_commit", max_files_per_commit, 30)
    if max_files_per_commit < 2:
        raise ValueError("max_files_per_commit must be at least 2 — a pair needs two files.")
    since_args, since_date = _since_args(repo, since)
    tracked = _head_files(repo)
    target = _repo_file(repo, file) if file else None
    if target is not None and target not in tracked:
        raise ValueError(
            f"'{file}' is not a file tracked at HEAD in {repo.root} — pass a path relative "
            "to the repository root (e.g. 'src/app.py')."
        )

    tracker = _RenameTracker()
    revisions: Counter = Counter()
    shared: Counter = Counter()
    analyzed = 0
    skipped = 0
    for _header, changes in _log_changes(repo, "--no-merges", *since_args):
        touched = {tracker.resolve(old, new) for old, new, _a, _d in changes}
        if len(touched) > max_files_per_commit:
            skipped += 1
            continue
        files = sorted(f for f in touched if f in tracked)
        if not files:
            continue
        analyzed += 1
        revisions.update(files)
        if target is not None:
            if target in files:
                for other in files:
                    if other != target:
                        shared[(target, other)] += 1
        else:
            for pair in combinations(files, 2):
                shared[pair] += 1

    def metrics(a: str, b: str, n: int) -> dict:
        ra, rb = revisions[a], revisions[b]
        return {
            "shared_commits": n,
            "degree": round(n / min(ra, rb), 2),
            "jaccard": round(n / (ra + rb - n), 2),
        }

    base = {
        **_base(repo),
        "since": since or "all history",
        "since_date": since_date,
        "commits_analyzed": analyzed,
        "commits_skipped_large": skipped,
        "min_shared": min_shared,
        "max_files_per_commit": max_files_per_commit,
    }
    if target is not None:
        partners = []
        for (_t, other), n in shared.items():
            if n < min_shared:
                continue
            partners.append({"file": other, **metrics(target, other, n),
                             "partner_revisions": revisions[other]})
        partners.sort(key=lambda p: (-p["degree"], -p["shared_commits"], -p["jaccard"], p["file"]))
        return {**base, "file": target, "revisions": revisions[target], "partners": partners[:top]}

    pairs = []
    for (a, b), n in shared.items():
        if n < min_shared or not (repo.in_scope(a) or repo.in_scope(b)):
            continue
        pairs.append({"file_a": a, "file_b": b, **metrics(a, b, n),
                      "revisions_a": revisions[a], "revisions_b": revisions[b]})
    pairs.sort(key=lambda p: (-p["degree"], -p["shared_commits"], -p["jaccard"], p["file_a"], p["file_b"]))
    return {**base, "pairs_found": len(pairs), "pairs": pairs[:top]}


def _author_activity(repo: Repo) -> dict[str, tuple[int, str]]:
    """Latest (timestamp, author-local YYYY-MM-DD) per mailmapped author.

    Merges count here: an integrator who still merges pull requests is still
    around. The date is the author's own calendar day, like ``%as`` in
    repo_summary, so the same commit never shows two different dates.
    """
    out = run_git(repo.root, "log", f"--pretty=format:%aN{FIELD_SEP}%at{FIELD_SEP}%as")
    last: dict[str, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(FIELD_SEP)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        name, stamp, day = parts[0], int(parts[1]), parts[2]
        if stamp > last.get(name, (-1, ""))[0]:
            last[name] = (stamp, day)
    return last


def _text_files(repo: Repo) -> tuple[list[str], int]:
    """Tracked non-empty text files, plus how many binary files were skipped."""
    out = run_git(repo.root, "ls-files", "-z", "--eol")
    files, binary = [], 0
    for entry in out.split("\0"):
        if "\t" not in entry:
            continue
        info, path = entry.split("\t", 1)
        if "i/-text" in info:
            binary += 1
        elif "i/none" not in info:  # i/none = empty file, nothing to own
            files.append(path)
    return files, binary


def _commit_frequency(repo: Repo) -> Counter:
    out = run_git(repo.root, "log", "--no-merges", "--no-renames", "--name-only", "-z", "--pretty=format:")
    return Counter(p.strip("\n") for p in out.split("\0") if p.strip("\n"))


def knowledge_risk(
    repo_path: str,
    inactive_after: str = "6 months ago",
    top: int = 20,
    path: str = "",
    max_files: int = 200,
) -> dict:
    """What breaks if someone leaves? Blames every tracked text file (under
    ``path`` / the scope; when there are more than ``max_files``, the most
    frequently changed ones) and compares line ownership with each author's
    last commit anywhere in the repository. Authors with no commit since
    ``inactive_after`` are inactive; a file where inactive authors own more
    than 50% of the lines is ``orphaned``. Returns the riskiest files, a
    per-author rollup (lines and files each person is the main owner of)
    and a per-directory rollup."""
    repo = open_repo(repo_path)
    top = _positive_int("top", top, 20)
    max_files = _positive_int("max_files", max_files, 200)
    if not isinstance(inactive_after, str) or not inactive_after.strip():
        raise ValueError(
            "inactive_after must be a git date such as '6 months ago' or '2025-01-01'."
        )
    cutoff = _resolve_date(repo, inactive_after, "inactive_after")

    scope = repo.prefix
    if path:
        norm = _repo_file(repo, path, allow_dir=True)
        if (Path(repo.root) / norm).is_file():
            scope = norm
        else:
            scope = norm + "/" if norm else ""
    group_prefix = scope if scope.endswith("/") or not scope else repo.prefix
    head = _head_files(repo)
    text_files, binary_skipped = _text_files(repo)
    candidates = [f for f in text_files if f in head and (f == scope or f.startswith(scope))]
    if not candidates:
        raise ValueError(
            f"No tracked text files under '{scope or '/'}' in {repo.root} — check 'path'."
        )
    considered = len(candidates)
    truncated = considered > max_files
    if truncated:
        freq = _commit_frequency(repo)
        candidates = sorted(candidates, key=lambda f: (-freq.get(f, 0), f))[:max_files]

    last_seen = _author_activity(repo)

    def blame_one(f: str) -> tuple[str, Counter]:
        try:
            return f, _blame(repo, f)[0]
        except ValueError:
            return f, Counter()

    workers = max(1, min(8, os.cpu_count() or 2, len(candidates)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        blamed = list(pool.map(blame_one, candidates))

    def active(author: str) -> bool:
        return last_seen.get(author, (0, ""))[0] >= cutoff

    files = []
    author_lines: Counter = Counter()
    author_files: Counter = Counter()
    dirs: dict[str, dict] = {}
    for f, counts in blamed:
        total = sum(counts.values())
        if total == 0:
            continue
        owners = _by_count_then_name(counts.items())
        main, main_lines = owners[0]
        inactive_lines = sum(n for a, n in owners if not active(a))
        inactive_pct = round(inactive_lines * 100 / total, 1)
        orphaned = inactive_pct > ORPHAN_PERCENT
        files.append(
            {
                "file": f,
                "lines": total,
                "main_owner": main,
                "main_owner_percent": round(main_lines * 100 / total, 1),
                "main_owner_active": active(main),
                "main_owner_last_commit": last_seen[main][1] if main in last_seen else None,
                "inactive_percent": inactive_pct,
                "orphaned": orphaned,
                "owners": [
                    {"author": a, "lines": n, "percent": round(n * 100 / total, 1)}
                    for a, n in owners[:3]
                ],
            }
        )
        author_lines.update(counts)
        author_files[main] += 1
        d = dirs.setdefault(
            _dir_key(group_prefix, f),
            {"files": 0, "lines": 0, "inactive_lines": 0, "orphaned_files": 0},
        )
        d["files"] += 1
        d["lines"] += total
        d["inactive_lines"] += inactive_lines
        d["orphaned_files"] += int(orphaned)

    # Riskiest first: most lines held by people who left, then the most lines
    # held by a single person (a big file only one person understands).
    files.sort(key=lambda r: (-r["inactive_percent"], -r["owners"][0]["lines"], -r["lines"], r["file"]))
    total_lines = sum(author_lines.values()) or 1
    authors = [
        {
            "author": a,
            "active": active(a),
            "last_commit": last_seen[a][1] if a in last_seen else None,
            "lines_owned": n,
            "lines_percent": round(n * 100 / total_lines, 1),
            "files_as_main_owner": author_files.get(a, 0),
        }
        for a, n in _by_count_then_name(author_lines.items())
    ]
    directories = [
        {
            "directory": d,
            "files": v["files"],
            "lines": v["lines"],
            "orphaned_files": v["orphaned_files"],
            "inactive_percent": round(v["inactive_lines"] * 100 / v["lines"], 1),
        }
        for d, v in sorted(dirs.items(), key=lambda kv: (-kv[1]["inactive_lines"], -kv[1]["lines"], kv[0]))
    ]
    return {
        **_base(repo),
        "path": scope or None,
        "inactive_after": inactive_after,
        "inactive_after_date": _day(cutoff),
        "inactive_authors": sorted(a for a in author_lines if not active(a)),
        "files_considered": considered,
        "files_analyzed": len(files),
        "orphaned_files": sum(1 for r in files if r["orphaned"]),
        "binary_files_skipped": binary_skipped,
        "truncated": truncated,
        "truncation_note": (
            f"only the {max_files} most frequently changed of {considered} files were "
            "blamed; raise max_files to analyse more" if truncated else None
        ),
        "files": files[:top],
        "authors": authors[:top],
        "directories": directories[:top],
        "orphan_rule": (
            f"orphaned when authors with no commit since {_day(cutoff)} own more than "
            f"{ORPHAN_PERCENT:g}% of the file's lines at HEAD"
        ),
    }


def _parse_name_status_z(out: str) -> dict[str, tuple[str, str | None, int | None]]:
    """Parse ``--name-status -z`` into {new_path: (status, old_path, similarity)}."""
    tokens = out.split("\0")
    result: dict[str, tuple[str, str | None, int | None]] = {}
    i = 0
    while i < len(tokens):
        status = tokens[i].strip("\n")
        i += 1
        if not status:
            continue
        code = status[0]
        if code in "RC":
            if i + 1 >= len(tokens):
                break
            old, new = tokens[i], tokens[i + 1]
            i += 2
            similarity = int(status[1:]) if status[1:].isdigit() else None
            result[new] = (code, old, similarity)
        else:
            if i >= len(tokens):
                break
            result[tokens[i]] = (code, None, None)
            i += 1
    return result


def commit_details(repo_path: str, ref: str) -> dict:
    """Everything about one commit: full message, author and committer (with
    dates), parents (merge detection), every file changed with its status
    (A added, M modified, D deleted, R renamed, C copied, T type change) and
    +/- lines, totals, and the branches and tags that contain it. For merge
    commits the diff is against the first parent, i.e. what the merge
    brought into the mainline."""
    repo = open_repo(repo_path)
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a commit hash, branch, tag or expression like 'HEAD~2'.")
    ref = ref.strip()
    if ref.startswith("-"):
        raise ValueError(f"ref must not start with '-' (got {ref!r}) — pass a commit hash or name.")
    full = _try_git(repo.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if not full or not full.strip():
        raise ValueError(
            f"Unknown commit '{ref}' in {repo.root} — pass a full or abbreviated hash, a "
            "branch or tag name, or an expression like 'HEAD~2'."
        )
    sha = full.strip()
    fmt = FIELD_SEP.join(["%H", "%h", "%P", "%aN", "%aE", "%aI", "%cN", "%cE", "%cI", "%s", "%b"])
    meta = run_git(repo.root, "show", "-s", f"--format={fmt}", sha).split(FIELD_SEP)
    full_hash, short, parents, a_name, a_mail, a_date, c_name, c_mail, c_date, subject = meta[:10]
    body = FIELD_SEP.join(meta[10:]).strip("\n")
    parent_list = parents.split()

    diff_args = ["diff-tree", "-r", "-M", "-z", "--no-commit-id"]
    target = [parent_list[0], sha] if parent_list else ["--root", sha]
    numstat = _parse_numstat_z(run_git(repo.root, *diff_args, "--numstat", *target))
    status = _parse_name_status_z(run_git(repo.root, *diff_args, "--name-status", *target))

    files = []
    insertions = deletions = 0
    for old, new, add, dele in numstat:
        code, old_path, similarity = status.get(new, ("M", None, None))
        entry: dict = {"path": new, "status": code, "lines_added": add, "lines_deleted": dele}
        if add is None:
            entry["binary"] = True
        if code in "RC":
            entry["old_path"] = old_path or old
            entry["similarity"] = similarity
        files.append(entry)
        insertions += add or 0
        deletions += dele or 0

    def refs(namespace: str) -> list[str]:
        # Full ref names so symbolic "refs/remotes/origin/HEAD" can be dropped
        # (its short form is just "origin", which reads like a branch).
        out = _try_git(repo.root, "for-each-ref", "--contains", sha,
                       "--format=%(refname)", namespace + "/")
        prefix = namespace + "/"
        return [n[len(prefix):] for n in (out or "").splitlines()
                if n.startswith(prefix) and not n.endswith("/HEAD")]

    branches = refs("refs/heads") + refs("refs/remotes")
    tags = refs("refs/tags")
    described = (_try_git(repo.root, "describe", "--contains", sha) or "").strip()
    if len(parent_list) > 1:
        diff_against = "first parent (what the merge brought into the mainline)"
    elif parent_list:
        diff_against = "parent"
    else:
        diff_against = "empty tree (root commit)"
    return {
        **_base(repo),
        "hash": full_hash,
        "short_hash": short,
        "subject": subject,
        "body": body,
        "author": {"name": a_name, "email": a_mail, "date": a_date},
        "committer": {"name": c_name, "email": c_mail, "date": c_date},
        "parents": parent_list,
        "is_merge": len(parent_list) > 1,
        "diff_against": diff_against,
        "stats": {"files_changed": len(files), "insertions": insertions, "deletions": deletions},
        "files": files[:MAX_COMMIT_FILES_LISTED],
        "files_truncated": len(files) > MAX_COMMIT_FILES_LISTED,
        "branches": branches[:MAX_REFS_LISTED],
        "branch_count": len(branches),
        "tags": tags[:MAX_REFS_LISTED],
        "tag_count": len(tags),
        "first_tag": described or None,
    }
