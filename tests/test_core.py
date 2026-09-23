"""Tests for core.py: build real git repos in a temp dir and interrogate them.

No mcp import anywhere: these tests must pass without the mcp package
installed. The fixtures live in conftest.py and pin every commit date.
"""

from pathlib import Path

import pytest
from conftest import git

from mcp_git_historian import core

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
    git(empty, "init", "-q", "-b", "main")
    with pytest.raises(ValueError, match="no commits yet"):
        core.repo_summary(str(empty))


def test_file_instead_of_directory_raises_clear_error(repo: Path):
    with pytest.raises(ValueError, match="is a file, not a directory"):
        core.repo_summary(str(repo / "src" / "app.py"))


def test_git_dir_itself_is_rejected(repo: Path):
    with pytest.raises(ValueError, match="not inside a git work tree"):
        core.repo_summary(str(repo / ".git"))


def test_timeout_is_configurable(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(core.TIMEOUT_ENV, "120")
    assert core.git_timeout() == 120.0
    monkeypatch.setenv(core.TIMEOUT_ENV, "0.001")  # far too short for any git call
    with pytest.raises(ValueError, match=r"timed out after 0\.001s.*GIT_HISTORIAN_TIMEOUT"):
        core.repo_summary(str(repo))
    monkeypatch.setenv(core.TIMEOUT_ENV, "soon")
    with pytest.raises(ValueError, match="not a positive number of seconds"):
        core.repo_summary(str(repo))
    monkeypatch.delenv(core.TIMEOUT_ENV)
    assert core.git_timeout() == core.GIT_TIMEOUT


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


def test_summary_mailmap_and_merges(forensics_repo: Path):
    s = core.repo_summary(str(forensics_repo))
    assert s["total_commits"] == 20
    assert s["merge_commits"] == 2
    names = [c["author"] for c in s["top_contributors"]]
    # "alice <alice@personal.dev>" is folded into Alice Dev by .mailmap and the
    # integrator who only merged pull requests is not a contributor
    assert names == ["Alice Dev", "Bob Ops", "Carol Gone"]
    assert s["top_contributors"][0]["commits"] == 9
    with_merges = core.repo_summary(str(forensics_repo), include_merges=True)
    assert {"author": "Maint Merger", "commits": 2} in with_merges["top_contributors"]


# ---------------------------------------------------------------------------
# hotspots
# ---------------------------------------------------------------------------

def test_hotspots_ranking_and_percentiles(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01")
    files = [e["file"] for e in h["hotspots"]]
    assert files[0] == "src/app.py"
    assert files[1] == "legacy/parser.py"
    top = h["hotspots"][0]
    assert top["commits"] == 4
    assert top["lines_added"] > 0
    assert top["churn_percentile"] == 83.3
    # Only three files changed: none of them is a statistical outlier, so the
    # 0.1.x behaviour of stamping the first three rows "high churn" is gone.
    assert all("hint" not in e for e in h["hotspots"])
    assert h["since_date"] == "2020-01-01"


def test_hotspots_excludes_deleted_files(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01")
    files = [e["file"] for e in h["hotspots"]]
    assert "old.txt" not in files
    assert "docs/notes.md" not in files  # old name after rename


def test_hotspots_respects_top(repo: Path):
    h = core.hotspots(str(repo), since="2020-01-01", top=1)
    assert len(h["hotspots"]) == 1


def test_hotspots_follow_renames(forensics_repo: Path):
    """Regression: commits made as src/util.py used to vanish after the rename."""
    h = core.hotspots(str(forensics_repo), since="")
    helpers = next(e for e in h["hotspots"] if e["file"] == "src/helpers.py")
    history = core.file_history(str(forensics_repo), "src/helpers.py")
    assert helpers["commits"] == history["count"] == 6
    assert helpers["renamed_from"] == ["src/util.py"]
    assert helpers["lines_added"] == 12
    assert "src/util.py" not in [e["file"] for e in h["hotspots"]]


def test_hotspot_hint_needs_real_churn(forensics_repo: Path):
    """Regression: .mailmap (1 commit) used to be flagged as 'high churn'."""
    h = core.hotspots(str(forensics_repo), since="", top=50)
    flagged = [e["file"] for e in h["hotspots"] if "hint" in e]
    assert flagged == ["src/app.py", "src/helpers.py", "src/api.py", "tests/test_api.py"]
    rows = {e["file"]: e for e in h["hotspots"]}
    assert rows["src/app.py"]["churn_percentile"] == 98.7
    assert rows[".mailmap"]["commits"] == 1 and "hint" not in rows[".mailmap"]
    assert rows["legacy/parser.py"]["commits"] == 3 and "hint" not in rows["legacy/parser.py"]
    assert h["files_changed"] == 38


def test_hotspots_on_subdirectory(forensics_repo: Path):
    """Regression: pointing repo_path at a sub-folder silently returned nothing."""
    h = core.hotspots(str(forensics_repo / "src"), since="")
    assert h["scope"] == "src/"
    assert [e["file"] for e in h["hotspots"]] == ["src/app.py", "src/helpers.py", "src/api.py"]
    assert h["hotspots"][0]["commits"] == 8


def test_hotspots_reject_bad_top_and_dates(repo: Path):
    with pytest.raises(ValueError, match="top must be at least 1"):
        core.hotspots(str(repo), top=-1)
    with pytest.raises(ValueError, match="top must be an integer"):
        core.hotspots(str(repo), top=True)
    with pytest.raises(ValueError, match="could not understand since='garbage'"):
        core.hotspots(str(repo), since="garbage")


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


def test_file_history_path_forms(forensics_repo: Path):
    """Root-relative, sub-folder-relative, absolute and Windows-style paths all resolve."""
    root_rel = core.file_history(str(forensics_repo), "src/app.py")
    from_subdir = core.file_history(str(forensics_repo / "src"), "app.py")
    absolute = core.file_history(str(forensics_repo), str(forensics_repo / "src" / "app.py"))
    backslash = core.file_history(str(forensics_repo), "src\\app.py")
    for result in (from_subdir, absolute, backslash):
        assert result["file"] == "src/app.py"
        assert result["count"] == root_rel["count"] == 8
    assert root_rel["commits"][-1]["author"] == "Alice Dev"
    with pytest.raises(ValueError, match="limit must be at least 1"):
        core.file_history(str(forensics_repo), "src/app.py", limit=0)


def test_file_history_rejects_paths_outside_repo(forensics_repo: Path, tmp_path: Path):
    with pytest.raises(ValueError, match="outside the repository"):
        core.file_history(str(forensics_repo), str(tmp_path / "elsewhere.py"))


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


def test_blame_summary_uses_mailmap(forensics_repo: Path):
    b = core.blame_summary(str(forensics_repo), "src/helpers.py")
    assert {a["author"] for a in b["authors"]} == {"Alice Dev", "Bob Ops"}
    assert b["total_lines"] == 10


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


def test_bus_factor_mailmap_merges_identities(forensics_repo: Path):
    """Regression: one person with two e-mail addresses counted as two authors."""
    bf = core.bus_factor(str(forensics_repo))
    names = [a["author"] for a in bf["top_authors"]]
    assert "alice" not in names
    assert names == ["Alice Dev", "Bob Ops", "Carol Gone"]
    assert bf["total_authors"] == 3
    assert bf["top_authors"][0] == {"author": "Alice Dev", "commits": 9, "percent": 50.0}
    assert bf["bus_factor"] == 2  # Alice's 50% is not *more* than half


def test_bus_factor_excludes_merge_only_integrators(forensics_repo: Path):
    """Regression: a maintainer who only merged PRs counted as a contributor."""
    bf = core.bus_factor(str(forensics_repo))
    assert bf["total_commits"] == 18
    assert bf["merge_commits_excluded"] == 2
    assert "Maint Merger" not in [a["author"] for a in bf["top_authors"]]
    with_merges = core.bus_factor(str(forensics_repo), include_merges=True)
    assert with_merges["total_commits"] == 20
    assert with_merges["total_authors"] == 4
    assert {"author": "Maint Merger", "commits": 2, "percent": 10.0} in with_merges["top_authors"]


def test_bus_factor_ignores_single_commit_directories(forensics_repo: Path):
    bf = core.bus_factor(str(forensics_repo))
    docs = next(d for d in bf["directories"] if d["directory"] == "docs")
    assert docs["commits"] == 1 and docs["dominant_percent"] == 100.0
    assert docs["knowledge_silo"] is False
    assert bf["knowledge_silos"] == []


def test_bus_factor_on_subdirectory(forensics_repo: Path):
    bf = core.bus_factor(str(forensics_repo / "legacy"))
    assert bf["scope"] == "legacy/"
    assert bf["total_commits"] == 4
    assert bf["top_authors"][0] == {"author": "Carol Gone", "commits": 3, "percent": 75.0}
    assert [d["directory"] for d in bf["directories"]] == ["legacy"]


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


def test_search_is_literal_by_default(forensics_repo: Path):
    """Regression: '[WIP' crashed git and 'fix: [WIP]' matched nothing."""
    unmatched = core.search_commits(str(forensics_repo), "[WIP")
    assert unmatched["count"] == 1
    assert unmatched["mode"] == "literal text"
    exact = core.search_commits(str(forensics_repo), "fix: [WIP]")
    assert [c["subject"] for c in exact["commits"]] == ["fix: [WIP] thing"]
    assert core.search_commits(str(forensics_repo), "a.i")["count"] == 0  # '.' is not a wildcard


def test_search_regex_mode(forensics_repo: Path):
    fixes = core.search_commits(str(forensics_repo), "^fix", regex=True)
    assert [c["subject"] for c in fixes["commits"]] == [
        "fix: [WIP] thing", "fix: api off-by-one", "fix: helpers edge case", "fix: lexer handles tabs",
    ]
    with pytest.raises(ValueError, match=r"Invalid regular expression '\[WIP'.*regex=false"):
        core.search_commits(str(forensics_repo), "[WIP", regex=True)


def test_search_author_filter_is_mailmap_aware(forensics_repo: Path):
    wired = core.search_commits(str(forensics_repo), "wire", author="Alice Dev")
    assert wired["count"] == 1
    assert wired["commits"][0]["author"] == "Alice Dev"  # committed as "alice"


def test_search_rejects_negative_limit(forensics_repo: Path):
    """Regression: limit=-1 used to mean 'unlimited' to git."""
    with pytest.raises(ValueError, match="limit must be at least 1"):
        core.search_commits(str(forensics_repo), "fix", limit=-1)


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


def test_find_change_regex_mode_and_rename_following(forensics_repo: Path):
    fc = core.find_change(str(forensics_repo), "slug(ify)?", file="src/helpers.py", regex=True)
    # both commits happened while the file was still called src/util.py
    assert [c["subject"] for c in fc["commits"]] == ["feat: util slugify", "feat: app skeleton"]
    assert fc["mode"].startswith("-G")
    with pytest.raises(ValueError, match="Invalid regular expression"):
        core.find_change(str(forensics_repo), "(", regex=True)
