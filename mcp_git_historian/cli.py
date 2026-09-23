"""Command-line interface: every MCP tool from a terminal, plus a Markdown report.

``mcp-git-historian`` with no arguments (or ``serve``) runs the stdio MCP
server exactly as before, so existing client configurations keep working.
Every other sub-command calls the same core functions as the MCP tools and
prints either a readable table or, with ``--json``, the raw tool result.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__, core
from .report import build_report


def _bar_glyph() -> str:
    """A full block where the console can print it, '#' on legacy code pages."""
    try:
        "█".encode(sys.stdout.encoding or "ascii")
        return "█"
    except (UnicodeEncodeError, LookupError):
        return "#"


def _bar(value: float, peak: float, width: int = 24) -> str:
    return _bar_glyph() * max(1 if value else 0, round(value / peak * width)) if peak else ""


def _table(headers: list[str], rows: list[list[object]], align: str = "") -> list[str]:
    align = align or "l" * len(headers)
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]

    def fmt(row: list[str]) -> str:
        parts = [c.rjust(w) if a == "r" else c.ljust(w) for c, w, a in zip(row, widths, align, strict=True)]
        return "  " + "  ".join(parts).rstrip()

    return [fmt(headers), "  " + "  ".join("-" * w for w in widths)] + [fmt(r) for r in cells]


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _where(result: dict) -> str:
    scope = f" (scope {result['scope']})" if result.get("scope") else ""
    return f"{result['repo_root']}{scope}"


def _commits(result: dict) -> list[str]:
    rows = [[c["hash"], c["date"], c["author"], c["subject"]] for c in result["commits"]]
    return _table(["hash", "date", "author", "subject"], rows) if rows else ["  (no matching commits)"]


# ---------------------------------------------------------------------------
# human-readable renderers (one per sub-command)
# ---------------------------------------------------------------------------

def render_summary(r: dict) -> list[str]:
    peak = max(r["activity_by_month"].values() or [0])
    out = [
        f"Repository  {_where(r)}",
        f"Branch      {r['branch']}",
        f"Commits     {r['total_commits']} (merges: {r['merge_commits']}), authors: {r['total_authors']}",
        f"First       {r['first_commit']['hash']}  {r['first_commit']['date']}",
        f"Last        {r['last_commit']['hash']}  {r['last_commit']['date']}",
        "",
        "Top contributors",
        *_table(["author", "commits"], [[c["author"], c["commits"]] for c in r["top_contributors"]], "lr"),
        "",
        "Activity by month",
    ]
    out += [f"  {m}  {n:>4}  {_bar(n, peak)}" for m, n in r["activity_by_month"].items()]
    return out + ["", r["note"]]


def render_hotspots(r: dict) -> list[str]:
    rows = []
    for h in r["hotspots"]:
        name = h["file"] + (f"  (was {', '.join(h['renamed_from'])})" if h.get("renamed_from") else "")
        rows.append([h["commits"], f"+{h['lines_added']}", f"-{h['lines_deleted']}",
                     f"{h['churn_percentile']:g}", "HOT" if "hint" in h else "", name])
    head = [f"Churn hotspots in {_where(r)} — {r['since']}"
            + (f" (since {r['since_date']})" if r["since_date"] else ""), ""]
    body = _table(["commits", "added", "deleted", "pctl", "flag", "file"], rows, "rrrrll") if rows \
        else ["  (no files changed in the window)"]
    return head + body + ["", f"{_n(r['files_changed'], 'file')} changed; {r['hint_rule']}."]


def render_history(r: dict) -> list[str]:
    rows = [[c["hash"], c["date"], c["author"], f"+{c['lines_added']}", f"-{c['lines_deleted']}",
             c["subject"] + (f"  (renamed from {c['renamed_from']})" if c.get("renamed_from") else "")]
            for c in r["commits"]]
    return [f"History of {r['file']} ({_n(r['count'], 'commit')})", ""] + \
        _table(["hash", "date", "author", "added", "deleted", "subject"], rows, "lllrrl")


def render_blame(r: dict) -> list[str]:
    if not r["total_lines"]:
        return [f"{r['file']} is empty at HEAD."]
    rows = [[a["author"], a["lines"], f"{a['percent']:g}%", _bar(a["percent"], 100)] for a in r["authors"]]
    return [
        f"Ownership of {r['file']} at HEAD ({r['total_lines']} lines)", "",
        *_table(["author", "lines", "share", ""], rows, "lrrl"), "",
        f"Oldest line {r['oldest_line']['date']} ({r['oldest_line']['author']}), "
        f"newest {r['newest_line']['date']} ({r['newest_line']['author']}).",
    ]


def render_bus_factor(r: dict) -> list[str]:
    authors = [[a["author"], a["commits"], f"{a['percent']:g}%"] for a in r["top_authors"]]
    dirs = [[d["directory"], d["commits"], d["authors"], d["dominant_author"], f"{d['dominant_percent']:g}%",
             "SILO" if d["knowledge_silo"] else ""] for d in r["directories"]]
    merges = "" if r["include_merges"] else f", merge commits excluded: {r['merge_commits_excluded']}"
    return [
        f"Bus factor {r['bus_factor']} in {_where(r)} (commits: {r['total_commits']}, "
        f"authors: {r['total_authors']}{merges})", "",
        *_table(["author", "commits", "share"], authors, "lrr"), "",
        *_table(["directory", "commits", "authors", "dominant author", "share", "flag"], dirs, "lrrlrl"), "",
        f"Silo rule: {r['silo_rule']}.",
    ]


def render_search(r: dict) -> list[str]:
    return [f"{_n(r['count'], 'commit')} matching {r['query']!r} ({r['mode']})", ""] + _commits(r)


def render_find_change(r: dict) -> list[str]:
    where = f" in {r['file']}" if r["file"] else ""
    return [f"{_n(r['count'], 'commit')} changed {r['pattern']!r}{where} ({r['mode']})", ""] + _commits(r)


def render_coupling(r: dict) -> list[str]:
    note = (f"{r['commits_analyzed']} commits analysed, {r['commits_skipped_large']} skipped for touching "
            f"more than {r['max_files_per_commit']} files")
    if "partners" in r:
        rows = [[p["shared_commits"], f"{p['degree']:.2f}", f"{p['jaccard']:.2f}", p["file"]] for p in r["partners"]]
        body = _table(["shared", "degree", "jaccard", "changes with"], rows, "rrrl") if rows \
            else [f"  (no partner shares {r['min_shared']}+ commits)"]
        return [f"Files coupled to {r['file']} ({r['revisions']} revisions)", ""] + body + ["", note + "."]
    rows = [[p["shared_commits"], f"{p['degree']:.2f}", f"{p['jaccard']:.2f}", f"{p['file_a']}  <->  {p['file_b']}"]
            for p in r["pairs"]]
    body = _table(["shared", "degree", "jaccard", "pair"], rows, "rrrl") if rows \
        else [f"  (no pair shares {r['min_shared']}+ commits)"]
    return [f"Change coupling in {_where(r)} — {r['since']}", ""] + body + \
        ["", f"{_n(r['pairs_found'], 'pair')} found; {note}."]


def render_knowledge_risk(r: dict) -> list[str]:
    files = [[f"{f['inactive_percent']:g}%", f"{f['main_owner']} ({f['main_owner_percent']:g}%)", f["lines"],
              "ORPHANED" if f["orphaned"] else "", f["file"]] for f in r["files"]]
    authors = [[a["author"], "yes" if a["active"] else "no", a["last_commit"], a["lines_owned"],
                f"{a['lines_percent']:g}%", a["files_as_main_owner"]] for a in r["authors"]]
    dirs = [[d["directory"], d["files"], d["lines"], d["orphaned_files"], f"{d['inactive_percent']:g}%"]
            for d in r["directories"]]
    out = [
        f"Knowledge-loss risk in {_where(r)} — inactive = no commit since {r['inactive_after_date']}",
        f"{r['orphaned_files']} orphaned of {r['files_analyzed']} files analysed; inactive authors: "
        + (", ".join(r["inactive_authors"]) or "none"),
        "",
        *_table(["inactive", "main owner", "lines", "flag", "file"], files, "rlrll"), "",
        *_table(["author", "active", "last commit", "lines", "share", "files owned"], authors, "llrrrr"), "",
        *_table(["directory", "files", "lines", "orphaned", "inactive"], dirs, "lrrrr"),
    ]
    if r["truncated"]:
        out += ["", f"Note: {r['truncation_note']}."]
    return out


def render_commit(r: dict) -> list[str]:
    a, c = r["author"], r["committer"]
    out = [
        f"commit {r['hash']}" + ("  (merge)" if r["is_merge"] else ""),
        f"Author:    {a['name']} <{a['email']}>  {a['date']}",
    ]
    if (c["name"], c["email"]) != (a["name"], a["email"]) or c["date"] != a["date"]:
        out.append(f"Committer: {c['name']} <{c['email']}>  {c['date']}")
    if r["parents"]:
        out.append("Parents:   " + " ".join(p[:12] for p in r["parents"]))
    out += ["", f"    {r['subject']}"]
    if r["body"]:
        out += [""] + [f"    {line}".rstrip() for line in r["body"].splitlines()]
    rows = []
    for f in r["files"]:
        path = f"{f['old_path']} -> {f['path']}" if f.get("old_path") else f["path"]
        added = "bin" if f.get("binary") else f"+{f['lines_added']}"
        deleted = "" if f.get("binary") else f"-{f['lines_deleted']}"
        rows.append([f["status"], added, deleted, path])
    s = r["stats"]
    out += ["", f"Changes vs {r['diff_against']}: {s['files_changed']} files, +{s['insertions']} -{s['deletions']}"]
    out += _table(["st", "added", "deleted", "path"], rows, "lrrl") if rows else []
    out += ["", "Branches:  " + (", ".join(r["branches"]) or "none"),
            "Tags:      " + (", ".join(r["tags"]) or "none")
            + (f"  (first: {r['first_tag']})" if r["first_tag"] else "")]
    return out


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _add_repo(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", help="path to a git repository or any folder inside it")
    p.add_argument("--json", action="store_true", help="print the raw tool result as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-git-historian",
        description="Git archaeology from the terminal. Without a sub-command, runs the MCP "
                    "server over stdio (what MCP clients launch).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("serve", help="run the MCP server over stdio (the default)")

    p = sub.add_parser("summary", help="branch, commits, contributors, monthly activity")
    _add_repo(p)
    p.add_argument("--include-merges", action="store_true", help="count merge commits as authored work")

    p = sub.add_parser("hotspots", help="most frequently changed files (rename-aware)")
    _add_repo(p)
    p.add_argument("--since", default="1 year ago", help='git date, "" for all history (default: 1 year ago)')
    p.add_argument("--top", type=int, default=15)

    p = sub.add_parser("history", help="commits that touched one file, following renames")
    _add_repo(p)
    p.add_argument("file", help="path relative to the repository root")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("blame", help="line ownership of one file at HEAD")
    _add_repo(p)
    p.add_argument("file", help="path relative to the repository root")

    p = sub.add_parser("bus-factor", help="bus factor and knowledge silos by directory")
    _add_repo(p)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--include-merges", action="store_true", help="count merge commits as authored work")

    p = sub.add_parser("search", help="search commit messages (literal text by default)")
    _add_repo(p)
    p.add_argument("query")
    p.add_argument("--author", default="", help="substring of the author name or e-mail")
    p.add_argument("--since", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--regex", action="store_true", help="treat QUERY as a POSIX extended regex")

    p = sub.add_parser("find-change", help="commits where a snippet appeared or disappeared (pickaxe)")
    _add_repo(p)
    p.add_argument("pattern")
    p.add_argument("--file", default="", help="restrict to one file (followed across renames)")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--regex", action="store_true", help="use git -G (regex on changed lines) instead of -S")

    p = sub.add_parser("coupling", help="files that keep changing together")
    _add_repo(p)
    p.add_argument("--file", default="", help="only the partners of this file")
    p.add_argument("--since", default="1 year ago")
    p.add_argument("--min-shared", type=int, default=3)
    p.add_argument("--max-files-per-commit", type=int, default=30)
    p.add_argument("--top", type=int, default=20)

    p = sub.add_parser("knowledge-risk", help="orphaned files and who owns what (blame-based)")
    _add_repo(p)
    p.add_argument("--inactive-after", default="6 months ago")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--path", default="", help="restrict to a folder or file")
    p.add_argument("--max-files", type=int, default=200)

    p = sub.add_parser("commit", help="everything about one commit")
    _add_repo(p)
    p.add_argument("ref", help="hash, branch, tag or expression such as HEAD~2")

    p = sub.add_parser("report", help="Markdown health report")
    p.add_argument("repo", help="path to a git repository or any folder inside it")
    p.add_argument("--since", default="1 year ago", help="churn/coupling window (default: 1 year ago)")
    p.add_argument("--inactive-after", default="6 months ago")
    p.add_argument("--top", type=int, default=10, help="rows per section")
    p.add_argument("--max-files", type=int, default=200, help="files to blame at most")
    p.add_argument("-o", "--output", help="write the report to this file instead of stdout")
    return parser


def _dispatch(args: argparse.Namespace) -> tuple[dict, Callable[[dict], list[str]]]:
    repo = args.repo
    cmd = args.command
    if cmd == "summary":
        return core.repo_summary(repo, include_merges=args.include_merges), render_summary
    if cmd == "hotspots":
        return core.hotspots(repo, since=args.since, top=args.top), render_hotspots
    if cmd == "history":
        return core.file_history(repo, args.file, limit=args.limit), render_history
    if cmd == "blame":
        return core.blame_summary(repo, args.file), render_blame
    if cmd == "bus-factor":
        return core.bus_factor(repo, top=args.top, include_merges=args.include_merges), render_bus_factor
    if cmd == "search":
        return core.search_commits(repo, args.query, author=args.author, since=args.since,
                                   limit=args.limit, regex=args.regex), render_search
    if cmd == "find-change":
        return core.find_change(repo, args.pattern, file=args.file, limit=args.limit,
                                regex=args.regex), render_find_change
    if cmd == "coupling":
        return core.change_coupling(repo, file=args.file, since=args.since, min_shared=args.min_shared,
                                    max_files_per_commit=args.max_files_per_commit,
                                    top=args.top), render_coupling
    if cmd == "knowledge-risk":
        return core.knowledge_risk(repo, inactive_after=args.inactive_after, top=args.top,
                                   path=args.path, max_files=args.max_files), render_knowledge_risk
    if cmd == "commit":
        return core.commit_details(repo, args.ref), render_commit
    raise AssertionError(cmd)  # pragma: no cover


def _serve() -> int:
    from .server import main as serve_main

    serve_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``mcp-git-historian`` console script."""
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        return _serve()

    for stream in (sys.stdout, sys.stderr):
        try:  # never crash on a console that cannot encode a character
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args.repo = str(Path(args.repo).expanduser().resolve())
    try:
        if args.command == "report":
            text = build_report(args.repo, since=args.since, inactive_after=args.inactive_after,
                                top=args.top, max_files=args.max_files)
            if args.output:
                Path(args.output).write_text(text, encoding="utf-8", newline="\n")
                print(f"Report written to {args.output}")
            else:
                print(text)
            return 0
        result, render = _dispatch(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("\n".join(render(result)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
