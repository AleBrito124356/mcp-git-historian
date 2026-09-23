# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.2.0 — unreleased

### Fixed

- **The server starts again on a fresh install.** `mcp>=1.2.0` resolved to mcp 2.x, which removed `mcp.server.fastmcp`, so the server crashed on import. It now uses `MCPServer` on mcp 2.x and `FastMCP` on 1.x. The dependency is `mcp>=1.10,<3`, tested with 1.10.0, 1.30.0 and 2.2.0.
- Tool errors keep their actionable message on mcp 2.x. That SDK hides the text of any exception that is not a `ToolError`, so every tool now converts core's `ValueError`s.
- `hotspots` follows renames: commits made under a file's former names count towards its current name (`renamed_from`). Previously a renamed file lost its whole earlier history.
- The "high churn" hint is no longer stamped on the first three rows whatever their churn. It requires ≥ 3 commits and a `churn_percentile` ≥ 90. Knowledge silos likewise require ≥ 3 commits.
- Author identities go through `.mailmap` in every tool, as `git blame` already did, so one person with two e-mail addresses is one author.
- Merge commits no longer count as authored work in `repo_summary` and `bus_factor` (opt back in with `include_merges=true`).
- Pointing `repo_path` at a folder inside a repository scopes the analysis to it instead of silently returning nothing. File arguments accept root-relative, folder-relative, absolute and backslash paths.
- `search_commits` treats the query as literal text, so `[WIP` no longer crashes git and `fix: [WIP]` matches. Use `regex=true` for POSIX extended regexes. Invalid regexes give a clear error.
- `limit` / `top` of zero or below are rejected instead of meaning "unlimited" or slicing from the end. Dates that git would silently read as "now" are rejected, and windowed results echo the resolved date.
- Parsing is hardened: NUL-separated `-z` output, explicit rename detection, pinned git config, `LC_ALL=C` and no optional locks.

### Added

- `change_coupling`: files that keep changing together (shared commits, degree, Jaccard), rename-aware, with mass commits filtered out.
- `knowledge_risk`: blame-based ownership against each author's last commit, with orphaned files and per-author and per-directory rollups.
- `commit_details`: full message, author/committer, parents, per-file status and line counts, old paths for renames, containing branches and tags.
- `health_report` MCP tool and `mcp-git-historian report`: a deterministic Markdown health report with a "what to look at first" ranking.
- A command-line interface: every tool is a sub-command with `--json` output, and `python -m mcp_git_historian` works too. With no arguments (or `serve`) the command still runs the stdio MCP server, so existing client configurations keep working.
- `regex=true` for `find_change` (git `-G`), plus `include_merges` for `repo_summary` and `bus_factor`.
- Read-only tool annotations, per-argument schema descriptions and server instructions for MCP clients.
- `GIT_HISTORIAN_TIMEOUT` environment variable to raise the per-call git timeout.
- 81 tests, up from 23: a second fixture repository reproduces every bug above, a CLI suite, and protocol-level server tests, both in-process through the SDK client and over real stdio with raw JSON-RPC.

### Changed

- `hotspots` rows carry `churn_percentile`, and results include `repo_root`, `scope` and `since_date`. `bus_factor` reports `merge_commits_excluded` and `silo_rule`. `repo_summary` reports `merge_commits` and `total_authors`.
- The `mcp-git-historian` console script now points at `mcp_git_historian.cli:main` (it still serves MCP by default).
- README: the PyPI / `uvx mcp-git-historian` instructions and badges were removed because the package has never been published. It now documents installing from GitHub, which works today.

## [0.1.0] — 2026-07-26

### Added

- Seven MCP tools over any local git repository: `repo_summary`, `hotspots`, `file_history`, `blame_summary`, `bus_factor`, `search_commits` and `find_change`.
- Churn hotspot ranking (commits + lines added/deleted per file) that excludes deleted files and flags the top 3 as refactor candidates.
- Ownership analysis: `blame_summary` reports the percentage of surviving lines per author, and `bus_factor` detects knowledge silos per top-level directory plus the global bus factor.
- Commit forensics: case-insensitive message search with author/date filters, and pickaxe search (`git log -S`) that pinpoints the commit where a snippet actually appeared or disappeared.
- Zero third-party runtime dependencies beyond `mcp`: everything shells out to the local `git` CLI (30s timeout, UTF-8 with `errors="replace"`) and parses machine-friendly output — `--pretty` formats with ASCII unit/record separators, `--numstat` and `blame --line-porcelain`. Nothing leaves the machine.
- 23 tests that build a real throwaway git repository (two authors, a rename, a deleted file, pinned commit dates) and run without importing `mcp`.

[0.1.0]: https://github.com/AleBrito124356/mcp-git-historian/releases/tag/v0.1.0
