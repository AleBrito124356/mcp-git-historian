"""Core git archaeology logic for mcp-git-historian.

Pure Python stdlib — every function shells out to the local ``git`` CLI via
``subprocess`` and parses machine-friendly output: custom ``--pretty`` formats
with ASCII unit/record separators (``%x1f`` / ``%x1e``), ``--numstat`` and
``git blame --line-porcelain``. No third-party dependencies.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

GIT_TIMEOUT = 30  # seconds
FIELD_SEP = "\x1f"  # ASCII unit separator — between fields of one commit
RECORD_SEP = "\x1e"  # ASCII record separator — between commits

# Record-per-commit format: hash, author date (YYYY-MM-DD), author name, subject.
_COMMIT_FMT = f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%as{FIELD_SEP}%an{FIELD_SEP}%s"

_HINT = "high churn — candidate for refactoring or extra review"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def run_git(repo_path: str, *args: str) -> str:
    """Run ``git -C <repo_path> <args>`` and return stdout as text.

    Raises ValueError with an actionable message when git is not installed,
    the path does not exist, the path is not a git repository, the repo has
    no commits yet, or the command fails/times out. Windows-safe: output is
    decoded as UTF-8 with ``errors="replace"``.
    """
    path = Path(repo_path).expanduser()
    if not path.exists():
        raise ValueError(
            f"Repo not found at {repo_path} — pass an absolute path to a local git repository."
        )
    cmd = ["git", "-C", str(path), "-c", "core.quotepath=off", *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError:
        raise ValueError(
            "git executable not found — install git and make sure it is on your PATH."
        ) from None
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"'git {args[0]}' timed out after {GIT_TIMEOUT}s on {repo_path} — "
            "the repository may be very large or on a slow disk."
        ) from None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        low = stderr.lower()
        if "not a git repository" in low:
            raise ValueError(
                f"{repo_path} is not a git repository — pass the root folder of a "
                "repo (the one containing .git)."
            )
        if "does not have any commits" in low or (
            "ambiguous argument 'head'" in low and "unknown revision" in low
        ):
            raise ValueError(
                f"The repository at {repo_path} has no commits yet — nothing to analyze."
            )
        raise ValueError(f"'git {args[0]}' failed: {stderr or 'unknown error'}")
    return result.stdout


def _ensure_repo(repo_path: str) -> None:
    """Validate that repo_path is inside a git work tree (clear error otherwise)."""
    out = run_git(repo_path, "rev-parse", "--is-inside-work-tree").strip()
    if out != "true":
        raise ValueError(
            f"{repo_path} is not inside a git work tree — bare repositories are "
            "not supported; pass a normal checkout."
        )


def _parse_commit_records(out: str) -> list[dict]:
    """Parse output produced with _COMMIT_FMT into a list of commit dicts."""
    commits = []
    for chunk in out.split(RECORD_SEP):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(FIELD_SEP)
        if len(parts) < 4:
            continue
        commits.append(
            {"hash": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]}
        )
    return commits


def _split_rename(numstat_path: str) -> tuple[str, str]:
    """Resolve a numstat path into (old_path, new_path).

    Handles git's rename notations ``dir/{old.py => new.py}`` and
    ``old.txt => new.txt``. Plain paths return (path, path).
    """
    if "{" in numstat_path and " => " in numstat_path and "}" in numstat_path:
        prefix, rest = numstat_path.split("{", 1)
        inner, suffix = rest.split("}", 1)
        old_part, _, new_part = inner.partition(" => ")
        old = (prefix + old_part + suffix).replace("//", "/")
        new = (prefix + new_part + suffix).replace("//", "/")
        return old, new
    if " => " in numstat_path:
        old, _, new = numstat_path.partition(" => ")
        return old, new
    return numstat_path, numstat_path


def _iter_numstat_chunks(out: str):
    """Yield (header_line, numstat_lines) per commit from RECORD_SEP-split log output."""
    for chunk in out.split(RECORD_SEP):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        yield lines[0], lines[1:]


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


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def repo_summary(repo_path: str) -> dict:
    """Overview of a repository: current branch, total commits, first/last
    commit (date + short hash), top 10 contributors by commit count, and
    commit counts per month for the 12 calendar months ending at the most
    recent commit."""
    _ensure_repo(repo_path)
    branch = run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    out = run_git(
        repo_path, "log", f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%an{FIELD_SEP}%as"
    )
    records = []
    for chunk in out.split(RECORD_SEP):
        chunk = chunk.strip()
        if chunk:
            records.append(chunk.split(FIELD_SEP))
    if not records:
        raise ValueError(
            f"The repository at {repo_path} has no commits yet — nothing to analyze."
        )

    newest, oldest = records[0], records[-1]
    author_counts = Counter(r[1] for r in records)
    month_counts = Counter(r[2][:7] for r in records)
    months = _last_n_months(newest[2][:7], 12)

    return {
        "repo_path": repo_path,
        "branch": branch,
        "total_commits": len(records),
        "first_commit": {"hash": oldest[0], "date": oldest[2]},
        "last_commit": {"hash": newest[0], "date": newest[2]},
        "top_contributors": [
            {"author": author, "commits": count}
            for author, count in author_counts.most_common(10)
        ],
        "activity_by_month": {m: month_counts.get(m, 0) for m in months},
    }


def hotspots(repo_path: str, since: str = "1 year ago", top: int = 15) -> dict:
    """Files ranked by change frequency (number of commits touching them)
    plus aggregated lines added/deleted from ``--numstat``. Files no longer
    tracked (deleted) are excluded. The top 3 entries carry a ``hint``
    marking them as high-churn refactoring/review candidates."""
    _ensure_repo(repo_path)
    tracked = set(run_git(repo_path, "ls-files").splitlines())
    args = ["log", "--numstat", f"--pretty=format:{RECORD_SEP}%H"]
    if since:
        args.append(f"--since={since}")
    out = run_git(repo_path, *args)

    commit_counts: Counter = Counter()
    added: Counter = Counter()
    deleted: Counter = Counter()
    for _header, numstat_lines in _iter_numstat_chunks(out):
        for line in numstat_lines:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, dele, raw_path = parts
            _old, path = _split_rename(raw_path)
            commit_counts[path] += 1
            if add != "-":
                added[path] += int(add)
            if dele != "-":
                deleted[path] += int(dele)

    live_files = [f for f in commit_counts if f in tracked]
    ranked = sorted(
        live_files, key=lambda f: (-commit_counts[f], -(added[f] + deleted[f]), f)
    )
    result = []
    for i, f in enumerate(ranked[:top]):
        entry = {
            "file": f,
            "commits": commit_counts[f],
            "lines_added": added[f],
            "lines_deleted": deleted[f],
        }
        if i < 3:
            entry["hint"] = _HINT
        result.append(entry)

    return {
        "repo_path": repo_path,
        "since": since or "all history",
        "files_changed": len(live_files),
        "hotspots": result,
    }


def file_history(repo_path: str, file: str, limit: int = 20) -> dict:
    """Commits that touched one file (newest first): short hash, date, author,
    subject and lines added/deleted per commit. Uses ``--follow`` so history
    is tracked across renames; rename commits carry a ``renamed_from`` key."""
    _ensure_repo(repo_path)
    out = run_git(
        repo_path,
        "log",
        "--follow",
        "-n",
        str(limit),
        "--numstat",
        f"--pretty=format:{RECORD_SEP}%h{FIELD_SEP}%as{FIELD_SEP}%an{FIELD_SEP}%s",
        "--",
        file,
    )
    commits = []
    for header, numstat_lines in _iter_numstat_chunks(out):
        parts = header.split(FIELD_SEP)
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
        for line in numstat_lines:
            nparts = line.split("\t")
            if len(nparts) != 3:
                continue
            add, dele, raw_path = nparts
            entry["lines_added"] = int(add) if add != "-" else 0
            entry["lines_deleted"] = int(dele) if dele != "-" else 0
            old, new = _split_rename(raw_path)
            if old != new:
                entry["renamed_from"] = old
        commits.append(entry)

    if not commits:
        raise ValueError(
            f"No commits found for '{file}' in {repo_path} — the path must be "
            "relative to the repository root (e.g. 'src/app.py')."
        )
    return {"repo_path": repo_path, "file": file, "count": len(commits), "commits": commits}


def blame_summary(repo_path: str, file: str) -> dict:
    """Line ownership of a file at HEAD via ``git blame --line-porcelain``:
    percentage of lines per author, the dominant author, and the dates of the
    oldest and newest surviving lines."""
    _ensure_repo(repo_path)
    out = run_git(repo_path, "blame", "--line-porcelain", "HEAD", "--", file)

    line_counts: Counter = Counter()
    oldest: tuple[int, str] | None = None
    newest: tuple[int, str] | None = None
    cur_author = ""
    cur_time = 0
    for line in out.splitlines():
        if line.startswith("author "):
            cur_author = line[len("author "):]
        elif line.startswith("author-time "):
            cur_time = int(line.split()[1])
        elif line.startswith("\t"):
            line_counts[cur_author] += 1
            if oldest is None or cur_time < oldest[0]:
                oldest = (cur_time, cur_author)
            if newest is None or cur_time > newest[0]:
                newest = (cur_time, cur_author)

    total = sum(line_counts.values())
    if total == 0:
        return {
            "repo_path": repo_path,
            "file": file,
            "total_lines": 0,
            "authors": [],
            "dominant_author": None,
            "oldest_line": None,
            "newest_line": None,
        }

    def _day(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")

    authors = [
        {"author": author, "lines": count, "percent": round(count * 100 / total, 1)}
        for author, count in line_counts.most_common()
    ]
    return {
        "repo_path": repo_path,
        "file": file,
        "total_lines": total,
        "authors": authors,
        "dominant_author": authors[0]["author"],
        "oldest_line": {"date": _day(oldest[0]), "author": oldest[1]},
        "newest_line": {"date": _day(newest[0]), "author": newest[1]},
    }


def bus_factor(repo_path: str, top: int = 10) -> dict:
    """Knowledge-concentration analysis. Per top-level directory: the dominant
    author and their share of commits touching it; directories where one
    author owns >80% of commits are flagged as knowledge silos. The global
    bus factor is the minimum number of authors whose commits together cover
    more than 50% of all commits (lower = riskier)."""
    _ensure_repo(repo_path)
    out = run_git(
        repo_path, "log", "--numstat", f"--pretty=format:{RECORD_SEP}%H{FIELD_SEP}%an"
    )
    author_commits: Counter = Counter()
    dir_authors: dict[str, Counter] = {}
    for header, numstat_lines in _iter_numstat_chunks(out):
        parts = header.split(FIELD_SEP)
        if len(parts) < 2:
            continue
        author = parts[1]
        author_commits[author] += 1
        dirs = set()
        for line in numstat_lines:
            nparts = line.split("\t")
            if len(nparts) != 3:
                continue
            _old, path = _split_rename(nparts[2])
            dirs.add(path.split("/", 1)[0] if "/" in path else "(root)")
        for d in dirs:
            dir_authors.setdefault(d, Counter())[author] += 1

    total = sum(author_commits.values())
    if total == 0:
        raise ValueError(
            f"The repository at {repo_path} has no commits yet — nothing to analyze."
        )

    covered = 0
    factor = 0
    for _author, count in author_commits.most_common():
        covered += count
        factor += 1
        if covered * 2 > total:  # strictly more than 50%
            break

    directories = []
    silos = []
    for d, counter in sorted(
        dir_authors.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])
    ):
        dir_total = sum(counter.values())
        dom_author, dom_count = counter.most_common(1)[0]
        dom_percent = round(dom_count * 100 / dir_total, 1)
        is_silo = dom_percent > 80
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
        "repo_path": repo_path,
        "bus_factor": factor,
        "bus_factor_explanation": (
            "minimum number of authors whose commits together cover >50% of all "
            "commits — lower means knowledge is concentrated in fewer people"
        ),
        "total_commits": total,
        "total_authors": len(author_commits),
        "top_authors": [
            {"author": a, "commits": c, "percent": round(c * 100 / total, 1)}
            for a, c in author_commits.most_common(top)
        ],
        "directories": directories[:top],
        "knowledge_silos": silos,
    }


def search_commits(
    repo_path: str, query: str, author: str = "", since: str = "", limit: int = 20
) -> dict:
    """Search commit messages with ``git log --grep`` (case-insensitive),
    optionally filtered by author and date (--since). Returns matching
    commits newest first."""
    _ensure_repo(repo_path)
    if not query.strip():
        raise ValueError(
            "query must be a non-empty string — pass a word or phrase to search "
            "commit messages for."
        )
    args = ["log", "-i", f"--grep={query}", "-n", str(limit), _COMMIT_FMT]
    if author:
        args.append(f"--author={author}")
    if since:
        args.append(f"--since={since}")
    commits = _parse_commit_records(run_git(repo_path, *args))
    return {
        "repo_path": repo_path,
        "query": query,
        "filters": {"author": author or None, "since": since or None},
        "count": len(commits),
        "commits": commits,
    }


def find_change(repo_path: str, pattern: str, file: str = "", limit: int = 10) -> dict:
    """Find the commits that introduced or removed a piece of code using git's
    pickaxe (``git log -S<pattern>``): it returns commits where the *number of
    occurrences* of the pattern changed — i.e. the code was added or deleted.
    This differs from ``-G<regex>``, which matches the regex against diff text
    and therefore also flags commits that merely moved or reindented matching
    lines. Use this to answer "when did this code appear/disappear?"."""
    _ensure_repo(repo_path)
    if not pattern:
        raise ValueError(
            "pattern must be a non-empty string — pass the code snippet or "
            "identifier to search for (e.g. 'MAGIC_TOKEN')."
        )
    args = ["log", f"-S{pattern}", "-n", str(limit), _COMMIT_FMT]
    if file:
        args.extend(["--", file])
    commits = _parse_commit_records(run_git(repo_path, *args))
    return {
        "repo_path": repo_path,
        "pattern": pattern,
        "file": file or None,
        "count": len(commits),
        "commits": commits,
        "note": (
            "Pickaxe (-S) matches commits where the occurrence count of the "
            "pattern changed (code added/removed). git's -G flag would instead "
            "regex-match diff lines, also catching moves within a file."
        ),
    }
