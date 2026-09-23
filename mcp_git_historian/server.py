"""mcp-git-historian: MCP server entry point.

Wiring only: every tool delegates to core.py, which shells out to the local
``git`` CLI. Run over stdio with ``mcp-git-historian`` or
``python -m mcp_git_historian.server``.

Works with both major versions of the official MCP Python SDK:

* mcp 2.x renamed ``FastMCP`` to ``mcp.server.mcpserver.MCPServer`` and only
  forwards the message of a *deliberate* ``ToolError`` to the model (any other
  exception becomes a bare "Error executing tool X"), and
* mcp 1.x ships ``mcp.server.fastmcp.FastMCP``.

core.py raises ``ValueError`` with actionable messages ("pass an absolute
path...", "limit must be at least 1..."), so every tool converts them into a
``ToolError`` and the model can read the hint and correct its call.
"""

import functools
import inspect
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError

    SDK_MAJOR = 2
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError

    SDK_MAJOR = 1

from mcp.types import ToolAnnotations

from . import __version__, core, report

INSTRUCTIONS = """\
Git archaeology over repositories on this machine. Every tool is read-only.
- Always pass repo_path as an ABSOLUTE path. It can be the repository root or
  any folder inside it; a sub-folder scopes the analysis to that folder.
- File arguments are relative to the repository root (e.g. "src/app.py");
  absolute paths inside the repository also work.
- Start with repo_summary, then hotspots (what changes most), change_coupling
  (what changes together), bus_factor / knowledge_risk (who knows what, and
  what is orphaned), and file_history / blame_summary / commit_details /
  find_change / search_commits to dig into specifics. health_report returns
  all of the overview in one Markdown document.
- Author names are .mailmap-resolved; merge commits are excluded from
  authorship and churn statistics unless include_merges is true.
- Dates such as since / inactive_after accept git date expressions:
  "6 months ago", "2 weeks ago", "2025-01-01". Results echo the resolved date.
"""

_kwargs: dict[str, Any] = {"instructions": INSTRUCTIONS}
if "version" in inspect.signature(_Server.__init__).parameters:
    _kwargs["version"] = __version__
mcp = _Server("mcp-git-historian", **_kwargs)

_READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)


def _tool(title: str) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    """Register a read-only tool whose ValueErrors reach the model verbatim."""

    def decorate(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> dict:
            try:
                return fn(*args, **kwargs)
            except ValueError as exc:
                raise ToolError(str(exc)) from None

        return mcp.tool(title=title, annotations=_READ_ONLY)(wrapper)

    return decorate


RepoPath = Annotated[
    str,
    Field(description="Absolute path to a local git repository, or to any folder inside one "
                      "(a sub-folder scopes the analysis to it)."),
]
RepoFile = Annotated[
    str,
    Field(description="File path relative to the repository root, e.g. 'src/app.py' "
                      "(an absolute path inside the repository also works)."),
]
Since = Annotated[
    str,
    Field(description="Only count commits newer than this git date expression, e.g. "
                      "'1 year ago', '6 months ago', '2025-01-01'. Empty string = all history."),
]
IncludeMerges = Annotated[
    bool,
    Field(description="Count merge commits as authored work (default false: merging a pull "
                      "request is not writing its code)."),
]


@_tool("Repository summary")
def repo_summary(repo_path: RepoPath, include_merges: IncludeMerges = False) -> dict[str, Any]:
    """Overview of a local git repository: current branch, total and merge
    commits, first and last commit (short hash + date), number of authors, top
    10 contributors by authored commits and commit activity per month for the
    12 calendar months ending at the most recent commit. Good first call."""
    return core.repo_summary(repo_path, include_merges=include_merges)


@_tool("Churn hotspots")
def hotspots(
    repo_path: RepoPath,
    since: Since = "1 year ago",
    top: Annotated[int, Field(description="Maximum number of files to return.", ge=1)] = 15,
) -> dict[str, Any]:
    """Rank the most frequently changed files (churn hotspots): commits
    touching each file and total lines added/deleted, following renames (older
    commits under a former name count towards the current name, listed in
    'renamed_from'). Deleted files and merge commits are excluded. Each row has
    'churn_percentile'; only files with >= 3 commits in the top decile carry
    a 'hint' marking them as refactoring / extra-review candidates."""
    return core.hotspots(repo_path, since=since, top=top)


@_tool("File history")
def file_history(
    repo_path: RepoPath,
    file: RepoFile,
    limit: Annotated[int, Field(description="Maximum number of commits to return.", ge=1)] = 20,
) -> dict[str, Any]:
    """List the commits that touched one file, newest first: short hash, date,
    author, subject, and lines added/deleted per commit. Follows the file
    across renames (rename commits include a 'renamed_from' key)."""
    return core.file_history(repo_path, file, limit=limit)


@_tool("Blame summary")
def blame_summary(repo_path: RepoPath, file: RepoFile) -> dict[str, Any]:
    """Summarize line ownership of a file at HEAD: percentage of surviving
    lines per author, the dominant author, and the dates of the oldest and
    newest lines. Answers "who wrote / who knows this file?"."""
    return core.blame_summary(repo_path, file)


@_tool("Bus factor and knowledge silos")
def bus_factor(
    repo_path: RepoPath,
    top: Annotated[int, Field(description="Maximum number of directories and authors to list.", ge=1)] = 10,
    include_merges: IncludeMerges = False,
) -> dict[str, Any]:
    """Analyze knowledge concentration by commits. Per top-level directory:
    dominant author and their share of commits; directories with >= 3 commits
    where one author made more than 80% of them are flagged as knowledge
    silos. Also returns the global bus factor: the minimum number of authors
    whose commits cover more than 50% of all commits (lower = riskier). For
    line-level ownership and inactive authors use knowledge_risk."""
    return core.bus_factor(repo_path, top=top, include_merges=include_merges)


@_tool("Search commit messages")
def search_commits(
    repo_path: RepoPath,
    query: Annotated[str, Field(description="Text to find in commit messages (subject or body). "
                                            "Literal by default: brackets, dots and stars need no escaping.")],
    author: Annotated[str, Field(description="Optional case-insensitive substring of the author's "
                                             "name or e-mail, e.g. 'alice'.")] = "",
    since: Annotated[str, Field(description="Optional date filter, e.g. '2 weeks ago', '2025-06-01'.")] = "",
    limit: Annotated[int, Field(description="Maximum number of commits to return.", ge=1)] = 20,
    regex: Annotated[bool, Field(description="Treat query as a POSIX extended regular expression "
                                             "instead of literal text.")] = False,
) -> dict[str, Any]:
    """Search commit messages case-insensitively, optionally filtered by
    author and date. Returns matches newest first (hash, date, author,
    subject)."""
    return core.search_commits(
        repo_path, query, author=author, since=since, limit=limit, regex=regex
    )


@_tool("Find when code appeared or disappeared")
def find_change(
    repo_path: RepoPath,
    pattern: Annotated[str, Field(description="Exact code snippet or identifier to track, e.g. "
                                              "'MAGIC_TOKEN' (a regex only when regex=true).")],
    file: Annotated[str, Field(description="Optional file (relative to the repository root) to "
                                           "restrict the search to; followed across renames.")] = "",
    limit: Annotated[int, Field(description="Maximum number of commits to return.", ge=1)] = 10,
    regex: Annotated[bool, Field(description="Use git's -G (regex matched against changed lines, also "
                                             "catches edits and moves) instead of the -S pickaxe.")] = False,
) -> dict[str, Any]:
    """Find when a piece of code appeared or disappeared using git's pickaxe
    (log -S): returns the commits where the number of occurrences of the
    pattern changed, i.e. the code was actually added or removed (unlike -G,
    which also matches lines that merely moved). Great for "when was this
    constant/function introduced?" forensics."""
    return core.find_change(repo_path, pattern, file=file, limit=limit, regex=regex)


@_tool("Change coupling")
def change_coupling(
    repo_path: RepoPath,
    file: Annotated[str, Field(description="Optional file (relative to the repository root): return "
                                           "only the files coupled to it.")] = "",
    since: Since = "1 year ago",
    min_shared: Annotated[int, Field(description="Minimum number of shared commits for a pair.", ge=1)] = 3,
    max_files_per_commit: Annotated[int, Field(description="Skip commits touching more files than this "
                                                           "(mass reformatting, vendoring).", ge=2)] = 30,
    top: Annotated[int, Field(description="Maximum number of pairs/partners to return.", ge=1)] = 20,
) -> dict[str, Any]:
    """Temporal coupling: files that keep changing in the same commits, a
    hidden dependency static analysis cannot see (e.g. code and the test or
    config that must always be edited with it). Per pair: shared commits,
    each file's revisions, degree = shared / min(revisions) (1.0 = every
    change to the rarer file also touched the other) and jaccard = shared /
    union. Rename-aware; merges and huge commits are ignored."""
    return core.change_coupling(
        repo_path, file=file, since=since, min_shared=min_shared,
        max_files_per_commit=max_files_per_commit, top=top,
    )


@_tool("Knowledge-loss risk")
def knowledge_risk(
    repo_path: RepoPath,
    inactive_after: Annotated[
        str,
        Field(description="Authors with no commit since this git date are considered gone, "
                          "e.g. '6 months ago', '2025-01-01'."),
    ] = "6 months ago",
    top: Annotated[int, Field(description="Maximum number of files, authors and directories to list.", ge=1)] = 20,
    path: Annotated[str, Field(description="Optional folder or file (relative to the repository root) "
                                           "to restrict the analysis to.")] = "",
    max_files: Annotated[int, Field(description="Blame at most this many files (the most frequently "
                                                "changed ones); the result says when it truncated.", ge=1)] = 200,
) -> dict[str, Any]:
    """What breaks if someone leaves? Blames the tracked text files and
    compares line ownership with each author's last commit. Files where
    inactive authors own more than 50% of the lines are 'orphaned'. Returns
    the riskiest files, a per-author rollup (lines owned, files they are the
    main owner of, last commit) and a per-directory rollup."""
    return core.knowledge_risk(
        repo_path, inactive_after=inactive_after, top=top, path=path, max_files=max_files
    )


@_tool("Commit details")
def commit_details(
    repo_path: RepoPath,
    ref: Annotated[str, Field(description="Commit to inspect: full or abbreviated hash, branch, tag, "
                                          "or an expression such as 'HEAD~2'.")],
) -> dict[str, Any]:
    """Inspect one commit: full message, author and committer with dates,
    parents (merge detection), every file changed with status (A/M/D/R/C/T),
    old path for renames and +/- lines, totals, and the branches and tags that
    contain it (plus the first tag that includes it). Merges are diffed
    against their first parent."""
    return core.commit_details(repo_path, ref)


@_tool("Health report")
def health_report(
    repo_path: RepoPath,
    since: Since = "1 year ago",
    inactive_after: Annotated[str, Field(description="Authors with no commit since this git date are "
                                                     "considered gone.")] = "6 months ago",
    top: Annotated[int, Field(description="Rows per section.", ge=1)] = 10,
    max_files: Annotated[int, Field(description="Blame at most this many files.", ge=1)] = 200,
) -> dict[str, Any]:
    """One-call Markdown health report combining repo_summary, hotspots,
    change_coupling, bus_factor and knowledge_risk, with a "what to look at
    first" list of files that change often AND are orphaned, single-owner or
    strongly coupled. Use it for an overview; use the individual tools to dig
    into details. Returns {"markdown": ...}."""
    markdown = report.build_report(
        repo_path, since=since, inactive_after=inactive_after, top=top, max_files=max_files
    )
    return {"repo_path": repo_path, "markdown": markdown}


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
