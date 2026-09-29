#!/usr/bin/env python3
"""Create Gitea pull mirrors for repositories owned by a GitHub user."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("github"))
