"""mcp-git-historian — MCP server entry point.

Wiring only: every tool delegates to core.py, which shells out to the local
``git`` CLI. Run over stdio: ``python server.py``.
"""

from mcp.server.fastmcp import FastMCP

import core

mcp = FastMCP("mcp-git-historian")


@mcp.tool()
def repo_summary(repo_path: str) -> dict:
    """Get an overview of a local git repository: current branch, total number
    of commits, first and last commit (short hash + date), top 10 contributors
    by commit count, and commit activity per month for the 12 calendar months
    ending at the most recent commit.

    Args:
        repo_path: Absolute path to a local git repository (its root folder).
    """
    return core.repo_summary(repo_path)


@mcp.tool()
def hotspots(repo_path: str, since: str = "1 year ago", top: int = 15) -> dict:
    """Rank the most frequently changed files (churn hotspots): number of
    commits touching each file plus total lines added/deleted. Deleted files
    are excluded; the top 3 entries carry a hint marking them as candidates
    for refactoring or extra review.

    Args:
        repo_path: Absolute path to a local git repository.
        since: Only count commits newer than this (any git date expression,
            e.g. "1 year ago", "6 months ago", "2025-01-01"). Empty string
            analyzes all history.
        top: Maximum number of files to return.
    """
    return core.hotspots(repo_path, since=since, top=top)


@mcp.tool()
def file_history(repo_path: str, file: str, limit: int = 20) -> dict:
    """List the commits that touched one file, newest first: short hash, date,
    author, subject, and lines added/deleted per commit. Follows the file
    across renames (rename commits include a 'renamed_from' key).

    Args:
        repo_path: Absolute path to a local git repository.
        file: File path relative to the repository root (e.g. "src/app.py").
        limit: Maximum number of commits to return.
    """
    return core.file_history(repo_path, file, limit=limit)


@mcp.tool()
def blame_summary(repo_path: str, file: str) -> dict:
    """Summarize line ownership of a file at HEAD: percentage of surviving
    lines per author, the dominant author, and the dates of the oldest and
    newest lines. Answers "who wrote / who knows this file?".

    Args:
        repo_path: Absolute path to a local git repository.
        file: File path relative to the repository root.
    """
    return core.blame_summary(repo_path, file)


@mcp.tool()
def bus_factor(repo_path: str, top: int = 10) -> dict:
    """Analyze knowledge concentration. Per top-level directory: dominant
    author and their share of commits; directories where one author owns more
    than 80% of commits are flagged as knowledge silos. Also returns the
    global bus factor: the minimum number of authors whose commits cover more
    than 50% of all commits (lower = riskier).

    Args:
        repo_path: Absolute path to a local git repository.
        top: Maximum number of directories and authors to list.
    """
    return core.bus_factor(repo_path, top=top)


@mcp.tool()
def search_commits(
    repo_path: str, query: str, author: str = "", since: str = "", limit: int = 20
) -> dict:
    """Search commit messages (case-insensitive, git log --grep), optionally
    filtered by author name and date. Returns matches newest first.

    Args:
        repo_path: Absolute path to a local git repository.
        query: Word or phrase to look for in commit messages.
        author: Optional author name filter (substring/regex, e.g. "Alice").
        since: Optional date filter (e.g. "2 weeks ago", "2025-06-01").
        limit: Maximum number of commits to return.
    """
    return core.search_commits(repo_path, query, author=author, since=since, limit=limit)


@mcp.tool()
def find_change(repo_path: str, pattern: str, file: str = "", limit: int = 10) -> dict:
    """Find when a piece of code appeared or disappeared using git's pickaxe
    (log -S): returns the commits where the number of occurrences of the
    pattern changed, i.e. the code was actually added or removed (unlike -G,
    which also matches lines that merely moved). Great for "when was this
    constant/function introduced?" forensics.

    Args:
        repo_path: Absolute path to a local git repository.
        pattern: Exact code snippet or identifier to track (not a regex).
        file: Optional file path to restrict the search to.
        limit: Maximum number of commits to return.
    """
    return core.find_change(repo_path, pattern, file=file, limit=limit)


if __name__ == "__main__":
    mcp.run()
