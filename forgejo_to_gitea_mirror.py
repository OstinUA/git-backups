#!/usr/bin/env python3
"""Create Gitea pull mirrors from a Forgejo instance."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("forgejo"))
