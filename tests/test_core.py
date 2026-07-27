"""Tests for core.py — build a real git repo in a temp dir and interrogate it.

No mcp import anywhere: these tests must pass without the mcp package
installed. Commit dates are pinned via GIT_AUTHOR_DATE / GIT_COMMITTER_DATE
for determinism.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_git_historian import core

ALICE = "Alice Dev <alice@example.com>"
BOB = "Bob Ops <bob@example.com>"


def _git(repo: Path, *args: str, date: str = "") -> None:
    env = os.environ.copy()
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _commit(repo: Path, message: str, author: str, date: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, f"--author={author}", date=date)


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


APP_V1 = """def main():
    print("hello")


if __name__ == "__main__":
    main()
"""

APP_V2 = """def main():
    print("hello")


def handle_empty(value):
    return value or ""


if __name__ == "__main__":
    main()
"""

APP_V3 = """def main():
    print("hello")


def handle_empty(value):
    return value or ""


def greet(name):
    return "Hi " + name


if __name__ == "__main__":
    main()
"""

APP_V4 = APP_V3.replace('"Hi " + name', '"Hello " + name')

PARSER_V1 = """MAGIC_TOKEN = "legacy-v1"


def parse(data):
    return data.split(",")
"""

PARSER_V2 = """MAGIC_TOKEN = "legacy-v1"


def parse(data):
    return [item.strip() for item in data.split(",")]
"""

PARSER_V3 = PARSER_V2 + """

def parse_unicode(data):
    return parse(data.encode("utf-8", "replace").decode("utf-8"))
"""


@pytest.fixture(scope="session")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real git repo: 10 commits, 2 authors, a rename, and a deleted file."""
    repo = tmp_path_factory.mktemp("histrepo")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "config", "user.email", "runner@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.autocrlf", "false")

    # 1 — Alice starts the app
    _write(repo, "src/app.py", APP_V1)
    _commit(repo, "feat: initial app", ALICE, "2026-01-05 10:00:00 +0000")
    # 2 — Alice adds the legacy parser (her private kingdom)
    _write(repo, "legacy/parser.py", PARSER_V1)
    _commit(repo, "feat: add legacy parser", ALICE, "2026-01-18 10:00:00 +0000")
    # 3 — Bob fixes a bug in the app
    _write(repo, "src/app.py", APP_V2)
    _commit(repo, "fix: handle empty input bug", BOB, "2026-02-03 10:00:00 +0000")
    # 4 — Alice adds docs
    _write(repo, "docs/notes.md", "# Notes\n\nSome notes.\n")
    _commit(repo, "docs: add notes", ALICE, "2026-02-15 10:00:00 +0000")
    # 5 — Alice touches the parser again
    _write(repo, "legacy/parser.py", PARSER_V2)
    _commit(repo, "refactor: tidy legacy parser", ALICE, "2026-03-01 10:00:00 +0000")
    # 6 — Bob adds a feature and a temp file at the repo root
    _write(repo, "src/app.py", APP_V3)
    _write(repo, "old.txt", "temporary\n")
    _commit(repo, "feat: add greet and temp file", BOB, "2026-03-12 10:00:00 +0000")
    # 7 — Alice renames the docs file
    _git(repo, "mv", "docs/notes.md", "docs/guide.md")
    _commit(repo, "docs: rename notes to guide", ALICE, "2026-04-02 10:00:00 +0000")
    # 8 — Bob deletes the temp file
    _git(repo, "rm", "-q", "old.txt")
    _commit(repo, "chore: remove temp file", BOB, "2026-04-20 10:00:00 +0000")
    # 9 — Alice fixes Bob's greeting
    _write(repo, "src/app.py", APP_V4)
    _commit(repo, "fix: bug in greeting punctuation", ALICE, "2026-05-06 10:00:00 +0000")
    # 10 — Alice extends the parser
    _write(repo, "legacy/parser.py", PARSER_V3)
    _commit(repo, "feat: parser handles unicode", ALICE, "2026-05-20 10:00:00 +0000")
    return repo


# ---------------------------------------------------------------------------
# run_git / error handling
# ---------------------------------------------------------------------------

def test_run_git_returns_output(repo: Path):
    assert core.run_git(str(repo), "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_missing_path_raises_clear_error():
    with pytest.raises(ValueError, match="Repo not found"):
        core.repo_summary(r"C:\definitely\does\not\exist-12345")


def test_non_repo_dir_raises_clear_error(tmp_path: Path):
    with pytest.raises(ValueError, match="not a git repository"):
        core.repo_summary(str(tmp_path))


def test_empty_repo_raises_clear_error(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")
    with pytest.raises(ValueError, match="no commits yet"):
        core.repo_summary(str(empty))


# ---------------------------------------------------------------------------
# repo_summary
# ---------------------------------------------------------------------------

def test_summary_counts(repo: Path):
    s = core.repo_summary(str(repo))
    assert s["branch"] == "main"
    assert s["total_commits"] == 10
    assert s["first_commit"]["date"] == "2026-01-05"
    assert s["last_commit"]["date"] == "2026-05-20"
    assert len(s["first_commit"]["hash"]) >= 7


def test_summary_contributors(repo: Path):
    s = core.repo_summary(str(repo))
    top = s["top_contributors"]
    assert top[0] == {"author": "Alice Dev", "commits": 7}
    assert top[1] == {"author": "Bob Ops", "commits": 3}


def test_summary_monthly_activity(repo: Path):
    s = core.repo_summary(str(repo))
    activity = s["activity_by_month"]
    assert len(activity) == 12
    # 12 months ending at the newest commit's month
    assert list(activity)[-1] == "2026-05"
    for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
        assert activity[month] == 2
    assert sum(activity.values()) == 10


# ---------------------------------------------------------------------------
# hotspots
# ---------------------------------------------------------------------------

def test_hotspots_ranking_and_hints(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01")
    files = [e["file"] for e in h["hotspots"]]
    assert files[0] == "src/app.py"
    assert files[1] == "legacy/parser.py"
    top = h["hotspots"][0]
    assert top["commits"] == 4
    assert top["lines_added"] > 0
    assert top["hint"] == "high churn — candidate for refactoring or extra review"


def test_hotspots_excludes_deleted_files(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01")
    files = [e["file"] for e in h["hotspots"]]
    assert "old.txt" not in files
    assert "docs/notes.md" not in files  # old name after rename


def test_hotspots_respects_top(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01", top=1)
    assert len(h["hotspots"]) == 1


# ---------------------------------------------------------------------------
# file_history
# ---------------------------------------------------------------------------

def test_file_history_follows_rename(repo: Path):
    fh = core.file_history(str(repo), "docs/guide.md")
    assert fh["count"] == 2  # rename commit + original add, thanks to --follow
    subjects = [c["subject"] for c in fh["commits"]]
    assert subjects == ["docs: rename notes to guide", "docs: add notes"]
    assert fh["commits"][0].get("renamed_from") == "docs/notes.md"


def test_file_history_newest_first_and_limit(repo: Path):
    fh = core.file_history(str(repo), "src/app.py", limit=2)
    assert fh["count"] == 2
    assert fh["commits"][0]["subject"] == "fix: bug in greeting punctuation"
    assert fh["commits"][0]["author"] == "Alice Dev"
    assert fh["commits"][0]["date"] == "2026-05-06"


def test_file_history_unknown_file_raises(repo: Path):
    with pytest.raises(ValueError, match="No commits found"):
        core.file_history(str(repo), "nope/missing.txt")


# ---------------------------------------------------------------------------
# blame_summary
# ---------------------------------------------------------------------------

def test_blame_summary_percentages(repo: Path):
    b = core.blame_summary(str(repo), "src/app.py")
    authors = {a["author"] for a in b["authors"]}
    assert authors == {"Alice Dev", "Bob Ops"}
    assert sum(a["lines"] for a in b["authors"]) == b["total_lines"]
    assert abs(sum(a["percent"] for a in b["authors"]) - 100.0) < 0.5
    assert b["dominant_author"] == b["authors"][0]["author"]


def test_blame_summary_line_dates(repo: Path):
    b = core.blame_summary(str(repo), "src/app.py")
    assert b["oldest_line"]["date"] == "2026-01-05"
    assert b["newest_line"]["date"] == "2026-05-06"
    assert b["newest_line"]["author"] == "Alice Dev"


def test_blame_summary_missing_file_raises(repo: Path):
    with pytest.raises(ValueError):
        core.blame_summary(str(repo), "nope/missing.txt")


# ---------------------------------------------------------------------------
# bus_factor
# ---------------------------------------------------------------------------

def test_bus_factor_global(repo: Path):
    bf = core.bus_factor(str(repo))
    # Alice alone has 7/10 = 70% > 50% of commits
    assert bf["bus_factor"] == 1
    assert bf["total_commits"] == 10
    assert bf["total_authors"] == 2
    assert bf["top_authors"][0]["author"] == "Alice Dev"
    assert bf["top_authors"][0]["percent"] == 70.0


def test_bus_factor_detects_silo(repo: Path):
    bf = core.bus_factor(str(repo))
    assert "legacy" in bf["knowledge_silos"]  # 100% Alice
    assert "src" not in bf["knowledge_silos"]  # 50/50 split
    legacy = next(d for d in bf["directories"] if d["directory"] == "legacy")
    assert legacy["dominant_author"] == "Alice Dev"
    assert legacy["dominant_percent"] == 100.0
    assert legacy["knowledge_silo"] is True
    src = next(d for d in bf["directories"] if d["directory"] == "src")
    assert src["dominant_percent"] == 50.0
    assert src["knowledge_silo"] is False


# ---------------------------------------------------------------------------
# search_commits
# ---------------------------------------------------------------------------

def test_search_is_case_insensitive(repo: Path):
    assert core.search_commits(str(repo), "bug")["count"] == 2
    assert core.search_commits(str(repo), "BUG")["count"] == 2


def test_search_author_and_since_filters(repo: Path):
    by_bob = core.search_commits(str(repo), "bug", author="Bob")
    assert by_bob["count"] == 1
    assert by_bob["commits"][0]["subject"] == "fix: handle empty input bug"
    recent = core.search_commits(str(repo), "bug", since="2026-05-01")
    assert recent["count"] == 1
    assert recent["commits"][0]["subject"] == "fix: bug in greeting punctuation"


def test_search_respects_limit_and_rejects_empty_query(repo: Path):
    assert core.search_commits(str(repo), "bug", limit=1)["count"] == 1
    with pytest.raises(ValueError, match="non-empty"):
        core.search_commits(str(repo), "   ")


# ---------------------------------------------------------------------------
# find_change
# ---------------------------------------------------------------------------

def test_find_change_locates_introduction(repo: Path):
    fc = core.find_change(str(repo), "MAGIC_TOKEN")
    assert fc["count"] == 1
    assert fc["commits"][0]["subject"] == "feat: add legacy parser"
    assert fc["commits"][0]["author"] == "Alice Dev"
    assert "-S" in fc["note"] and "-G" in fc["note"]


def test_find_change_with_file_filter(repo: Path):
    fc = core.find_change(str(repo), "greet", file="src/app.py")
    # -S only fires where the occurrence count changed (commit 6), not where
    # the line was edited without changing the count (commit 9)
    assert fc["count"] == 1
    assert fc["commits"][0]["subject"] == "feat: add greet and temp file"
    with pytest.raises(ValueError, match="non-empty"):
        core.find_change(str(repo), "")
