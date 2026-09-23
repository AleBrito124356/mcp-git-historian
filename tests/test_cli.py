"""Tests for the command-line interface and the Markdown report.

They call ``cli.main([...])`` in-process, so no console script needs to be
installed, and they never import ``mcp`` (``serve`` is covered by the stdio
test in test_server.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import rev

from mcp_git_historian import __version__, cli
from mcp_git_historian.report import build_report, sparkline


def run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def run_json(capsys: pytest.CaptureFixture, *argv: str) -> dict:
    code, out, err = run(capsys, *argv, "--json")
    assert code == 0, err
    return json.loads(out)


@pytest.mark.parametrize(
    ("argv", "key", "expected"),
    [
        (["summary"], "total_authors", 3),
        (["summary", "--include-merges"], "total_authors", 4),
        (["hotspots", "--since", "", "--top", "2"], "files_changed", 38),
        (["history", "src/helpers.py"], "count", 6),
        (["blame", "legacy/lexer.py"], "dominant_author", "Carol Gone"),
        (["bus-factor"], "bus_factor", 2),
        (["search", "[WIP"], "count", 1),
        (["search", "^fix", "--regex", "--author", "bob"], "count", 2),
        (["find-change", "MAGIC_TOKEN", "--file", "legacy/parser.py"], "count", 1),
        (["coupling", "--since", ""], "pairs_found", 2),
        (["coupling", "--since", "", "--file", "src/api.py"], "revisions", 4),
        (["knowledge-risk", "--inactive-after", "2026-01-01"], "orphaned_files", 2),
        (["knowledge-risk", "--inactive-after", "2026-01-01", "--path", "legacy", "--max-files", "1"],
         "truncated", True),
        (["commit", "v1.0.0"], "subject", "fix: api off-by-one"),
    ],
    ids=lambda v: " ".join(v) if isinstance(v, list) else None,
)
def test_json_output_matches_the_tools(capsys, forensics_repo: Path, argv, key, expected):
    command, *rest = argv
    data = run_json(capsys, command, str(forensics_repo), *rest)
    assert data[key] == expected
    assert data["repo_root"].endswith(forensics_repo.name)


def test_human_output_is_readable(capsys, forensics_repo: Path):
    repo = str(forensics_repo)
    _, out, _ = run(capsys, "hotspots", repo, "--since", "", "--top", "3")
    assert "src/app.py" in out and "HOT" in out and "(was src/util.py)" in out
    _, out, _ = run(capsys, "coupling", repo, "--since", "")
    assert "src/api.py  <->  tests/test_api.py" in out and "1 skipped" in out
    _, out, _ = run(capsys, "knowledge-risk", repo, "--inactive-after", "2026-01-01")
    assert "ORPHANED" in out and "inactive authors: Carol Gone" in out
    _, out, _ = run(capsys, "commit", repo, rev(forensics_repo, "refactor: rename util to helpers"))
    assert "src/util.py -> src/helpers.py" in out and "Tags:      v1.0.0" in out
    _, out, _ = run(capsys, "summary", repo)
    assert "Alice Dev" in out and "alice " not in out and "merges: 2" in out
    _, out, _ = run(capsys, "blame", repo, "src/app.py")
    assert "Ownership of src/app.py at HEAD" in out
    _, out, _ = run(capsys, "history", repo, "src/helpers.py", "--limit", "2")
    assert "perf: faster slugify" in out
    _, out, _ = run(capsys, "search", repo, "nothing-matches-this")
    assert "(no matching commits)" in out


def test_errors_exit_non_zero_with_the_tool_message(capsys, forensics_repo: Path, tmp_path: Path):
    code, out, err = run(capsys, "summary", str(tmp_path / "missing"))
    assert code == 1 and out == ""
    assert err.startswith("error: Repo not found")
    code, _, err = run(capsys, "search", str(forensics_repo), "fix", "--limit", "-1")
    assert code == 1 and "limit must be at least 1" in err
    code, _, err = run(capsys, "commit", str(forensics_repo), "no-such-ref")
    assert code == 1 and "Unknown commit 'no-such-ref'" in err


def test_usage_errors_and_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["hotspots"])  # missing repo argument
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_report_sections_and_attention_ranking(forensics_repo: Path):
    md = build_report(str(forensics_repo), since="", inactive_after="2026-01-01")
    for heading in ("# Git health report: ", "## Summary", "## Activity", "## What to look at first",
                    "## Churn hotspots", "## Change coupling", "## Knowledge", "### Orphaned files",
                    "### Who owns the code"):
        assert heading in md, heading
    assert "- **20 commits** (2 merges)" in md and "**3 authors**" in md
    assert "**2 orphaned files** of 38 analysed" in md
    assert "| `src/api.py` | `tests/test_api.py` | 4 | 1.00 | 1.00 |" in md
    assert "| `legacy/lexer.py` | 9 | Carol Gone | 2025-06-20 | 100% |" in md
    # orphaned + churned outranks merely coupled files
    attention = md.split("## What to look at first")[1].split("##")[0]
    first = [line for line in attention.splitlines() if line.startswith("1. ")][0]
    assert first.startswith("1. `legacy/parser.py`") and "inactive authors" in first
    assert "`src/app.py`" in attention and "degree 0.50" in attention
    # deterministic and free of local absolute paths
    assert md == build_report(str(forensics_repo), since="", inactive_after="2026-01-01")
    assert str(forensics_repo.parent) not in md


def test_report_subcommand_writes_file(capsys, forensics_repo: Path, tmp_path: Path):
    target = tmp_path / "health.md"
    code, out, _ = run(capsys, "report", str(forensics_repo), "--since", "", "--inactive-after",
                       "2026-01-01", "-o", str(target))
    assert code == 0 and "Report written to" in out
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Git health report: ")
    code, out, _ = run(capsys, "report", str(forensics_repo), "--since", "", "--inactive-after", "2026-01-01")
    assert code == 0 and out.strip() == text.strip()


def test_report_on_subdirectory_is_scoped(forensics_repo: Path):
    md = build_report(str(forensics_repo / "legacy"), since="", inactive_after="2026-01-01")
    assert "— scope `legacy/`" in md
    assert "src/app.py" not in md


def test_sparkline():
    assert sparkline([0, 0, 0]) == "▁▁▁"
    assert sparkline([0, 7, 14]) == "▁▅█"  # 7/14 of the way up the 8 levels
    assert sparkline([0, 1, 2, 3, 4, 5, 6, 7]) == "▁▂▃▄▅▆▇█"
