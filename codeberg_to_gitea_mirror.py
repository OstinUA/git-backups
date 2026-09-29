#!/usr/bin/env python3
"""Create Gitea pull mirrors for a Codeberg user or organization."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("codeberg"))
