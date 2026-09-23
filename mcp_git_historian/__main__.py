"""``python -m mcp_git_historian``: same as the ``mcp-git-historian`` command."""

import sys

from .cli import main

sys.exit(main())
