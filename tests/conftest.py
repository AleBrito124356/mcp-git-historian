"""Shared fixtures: real throwaway git repositories with pinned dates.

Nothing here imports ``mcp``, so the core and CLI tests run without the SDK.
Commit dates are pinned via GIT_AUTHOR_DATE / GIT_COMMITTER_DATE so every
number the tests assert is deterministic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ALICE = "Alice Dev <alice@example.com>"
ALICE_ALT = "alice <alice@personal.dev>"  # same person, other laptop
BOB = "Bob Ops <bob@example.com>"
CAROL = "Carol Gone <carol@example.com>"  # left the project in mid-2025
MAINT = ("Maint Merger", "maint@example.com")  # only ever merges pull requests


def git(repo: Path, *args: str, date: str = "", env_extra: dict | None = None) -> str:
    env = os.environ.copy()
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    env.update(env_extra or {})
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def commit(repo: Path, message: str, author: str, date: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, f"--author={author}", date=date)


def write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def init_repo(repo: Path) -> None:
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test Runner")
    git(repo, "config", "user.email", "runner@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")


# ---------------------------------------------------------------------------
# the original 10-commit fixture (kept byte-for-byte so 0.1.x assertions hold)
# ---------------------------------------------------------------------------

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
    init_repo(repo)

    # 1 — Alice starts the app
    write(repo, "src/app.py", APP_V1)
    commit(repo, "feat: initial app", ALICE, "2026-01-05 10:00:00 +0000")
    # 2 — Alice adds the legacy parser (her private kingdom)
    write(repo, "legacy/parser.py", PARSER_V1)
    commit(repo, "feat: add legacy parser", ALICE, "2026-01-18 10:00:00 +0000")
    # 3 — Bob fixes a bug in the app
    write(repo, "src/app.py", APP_V2)
    commit(repo, "fix: handle empty input bug", BOB, "2026-02-03 10:00:00 +0000")
    # 4 — Alice adds docs
    write(repo, "docs/notes.md", "# Notes\n\nSome notes.\n")
    commit(repo, "docs: add notes", ALICE, "2026-02-15 10:00:00 +0000")
    # 5 — Alice touches the parser again
    write(repo, "legacy/parser.py", PARSER_V2)
    commit(repo, "refactor: tidy legacy parser", ALICE, "2026-03-01 10:00:00 +0000")
    # 6 — Bob adds a feature and a temp file at the repo root
    write(repo, "src/app.py", APP_V3)
    write(repo, "old.txt", "temporary\n")
    commit(repo, "feat: add greet and temp file", BOB, "2026-03-12 10:00:00 +0000")
    # 7 — Alice renames the docs file
    git(repo, "mv", "docs/notes.md", "docs/guide.md")
    commit(repo, "docs: rename notes to guide", ALICE, "2026-04-02 10:00:00 +0000")
    # 8 — Bob deletes the temp file
    git(repo, "rm", "-q", "old.txt")
    commit(repo, "chore: remove temp file", BOB, "2026-04-20 10:00:00 +0000")
    # 9 — Alice fixes Bob's greeting
    write(repo, "src/app.py", APP_V4)
    commit(repo, "fix: bug in greeting punctuation", ALICE, "2026-05-06 10:00:00 +0000")
    # 10 — Alice extends the parser
    write(repo, "legacy/parser.py", PARSER_V3)
    commit(repo, "feat: parser handles unicode", ALICE, "2026-05-20 10:00:00 +0000")
    return repo


# ---------------------------------------------------------------------------
# the forensics fixture: every trap the 0.1.x analysis fell into
# ---------------------------------------------------------------------------

LEXER_V1 = 'TAB = "\\t"\n\n\ndef tokens(text):\n    return text.split()\n'
LEXER_V2 = LEXER_V1 + '\n\ndef tokens_keep_tabs(text):\n    return text.replace(TAB, " ").split()\n'
UTIL_V1 = "def slug(text):\n    return text.lower()\n"
UTIL_V2 = UTIL_V1 + '\n\ndef slugify(text):\n    return "-".join(text.lower().split())\n'
UTIL_V3 = UTIL_V2 + "\n\ndef title(text):\n    return text.title()\n"
HELPERS_V4 = UTIL_V3.replace("return text.lower()", "return text.strip().lower()")
HELPERS_V5 = HELPERS_V4.replace('"-".join(text.lower().split())', '"-".join(text.split()).lower()')
MAILMAP = "Alice Dev <alice@example.com> <alice@personal.dev>\n"


def _numbered(kind: str, n: int) -> str:
    """Source that grows by one function per version (distinct lines per author)."""
    body = f"# {kind}\n"
    for i in range(1, n + 1):
        body += f"\n\ndef {kind}_step_{i}():\n    return {i}\n"
    return body


def app(n: int) -> str:
    return _numbered("app", n)


def api(n: int) -> str:
    return _numbered("api", n)


def api_tests(n: int) -> str:
    return _numbered("test_api", n)


def _merge(repo: Path, branch: str, date: str) -> None:
    name, email = MAINT
    git(
        repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge branch '{branch}'",
        date=date,
        env_extra={
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        },
    )


@pytest.fixture(scope="session")
def forensics_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """20 commits: 18 authored + 2 merge-only commits by an integrator.

    * Carol wrote legacy/ and has not committed since 2025-06-20.
    * Alice commits under two identities, reconciled by .mailmap.
    * src/util.py is renamed to src/helpers.py and edited afterwards.
    * src/api.py and tests/test_api.py always change together.
    * One mass commit touches 32 files (licence headers + 30 data files).
    * One message contains the literal text "[WIP]".
    * Tag v1.0.0 points at "fix: api off-by-one".
    """
    r = tmp_path_factory.mktemp("forensics")
    init_repo(r)

    write(r, "legacy/parser.py", PARSER_V1)
    write(r, "legacy/lexer.py", LEXER_V1)
    commit(r, "feat: legacy parser and lexer", CAROL, "2025-03-01 10:00:00 +0000")
    write(r, "legacy/parser.py", PARSER_V2)
    commit(r, "refactor: tidy parser", CAROL, "2025-04-10 10:00:00 +0000")
    write(r, "src/app.py", app(1))
    write(r, "src/util.py", UTIL_V1)
    commit(r, "feat: app skeleton", ALICE, "2025-05-15 10:00:00 +0000")
    write(r, "legacy/lexer.py", LEXER_V2)
    commit(r, "fix: lexer handles tabs", CAROL, "2025-06-20 10:00:00 +0000")
    write(r, "src/util.py", UTIL_V2)
    commit(r, "feat: util slugify", ALICE, "2026-01-10 10:00:00 +0000")
    write(r, "src/util.py", UTIL_V3)
    write(r, "src/app.py", app(2))
    commit(r, "feat: wire util into app", ALICE_ALT, "2026-01-20 10:00:00 +0000")
    git(r, "mv", "src/util.py", "src/helpers.py")
    write(r, ".mailmap", MAILMAP)
    commit(r, "refactor: rename util to helpers", ALICE, "2026-02-01 10:00:00 +0000")
    write(r, "src/helpers.py", HELPERS_V4)
    write(r, "src/app.py", app(3))
    commit(r, "fix: helpers edge case", BOB, "2026-02-05 10:00:00 +0000")
    write(r, "src/api.py", api(1))
    write(r, "tests/test_api.py", api_tests(1))
    commit(r, "feat: api endpoint with tests", BOB, "2026-02-10 10:00:00 +0000")
    write(r, "src/api.py", api(2))
    write(r, "tests/test_api.py", api_tests(2))
    write(r, "src/app.py", app(4))
    commit(r, "feat: api pagination", ALICE, "2026-02-20 10:00:00 +0000")
    write(r, "src/api.py", api(3))
    write(r, "tests/test_api.py", api_tests(3))
    commit(r, "fix: api off-by-one", BOB, "2026-03-01 10:00:00 +0000")
    git(r, "tag", "-a", "v1.0.0", "-m", "release 1.0.0", date="2026-03-01 12:00:00 +0000")

    git(r, "checkout", "-q", "-b", "feature/greeting")
    write(r, "src/app.py", app(5))
    commit(r, "feat: greeting endpoint", BOB, "2026-03-10 10:00:00 +0000")
    write(r, "src/api.py", api(4))
    write(r, "tests/test_api.py", api_tests(4))
    commit(r, "feat: api v2 greeting route", BOB, "2026-03-12 10:00:00 +0000")
    git(r, "checkout", "-q", "main")
    _merge(r, "feature/greeting", "2026-03-15 10:00:00 +0000")

    git(r, "checkout", "-q", "-b", "feature/docs")
    write(r, "docs/guide.md", "# Guide\n\nHow to run the app.\n")
    commit(r, "docs: add guide", ALICE_ALT, "2026-03-20 10:00:00 +0000")
    git(r, "checkout", "-q", "main")
    _merge(r, "feature/docs", "2026-03-22 10:00:00 +0000")

    write(r, "src/app.py", app(6))
    commit(r, "fix: [WIP] thing", ALICE, "2026-04-01 10:00:00 +0000")
    for i in range(30):
        write(r, f"assets/data_{i:02d}.txt", f"data {i}\n")
    write(r, "src/app.py", "# SPDX-License-Identifier: MIT\n" + app(6))
    write(r, "legacy/parser.py", "# SPDX-License-Identifier: MIT\n" + PARSER_V2)
    commit(r, "chore: add licence headers and data files", ALICE, "2026-04-15 10:00:00 +0000")
    write(r, "src/helpers.py", HELPERS_V5)
    commit(r, "perf: faster slugify", BOB, "2026-05-01 10:00:00 +0000")
    write(r, "src/app.py", "# SPDX-License-Identifier: MIT\n" + app(7))
    commit(r, "feat: app config", ALICE, "2026-05-10 10:00:00 +0000")
    return r


def rev(repo: Path, message: str) -> str:
    """Full hash of the (unique) commit whose subject is ``message``."""
    out = git(repo, "log", "--all", "--format=%H%x1f%s")
    for line in out.splitlines():
        sha, _, subject = line.partition("\x1f")
        if subject == message:
            return sha
    raise LookupError(message)
