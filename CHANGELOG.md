# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-26

### Added

- Seven MCP tools over any local git repository: `repo_summary`, `hotspots`, `file_history`, `blame_summary`, `bus_factor`, `search_commits` and `find_change`.
- Churn hotspot ranking (commits + lines added/deleted per file) that excludes deleted files and flags the top 3 as refactor candidates.
- Ownership analysis: `blame_summary` reports the percentage of surviving lines per author, and `bus_factor` detects knowledge silos per top-level directory plus the global bus factor.
- Commit forensics: case-insensitive message search with author/date filters, and pickaxe search (`git log -S`) that pinpoints the commit where a snippet actually appeared or disappeared.
- Zero third-party runtime dependencies beyond `mcp`: everything shells out to the local `git` CLI (30s timeout, UTF-8 with `errors="replace"`) and parses machine-friendly output — `--pretty` formats with ASCII unit/record separators, `--numstat` and `blame --line-porcelain`. Nothing leaves the machine.
- 23 tests that build a real throwaway git repository (two authors, a rename, a deleted file, pinned commit dates) and run without importing `mcp`.

[0.1.0]: https://github.com/AleBrito124356/mcp-git-historian/releases/tag/v0.1.0
