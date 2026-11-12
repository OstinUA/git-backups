#!/usr/bin/env python3
"""Create Gitea pull mirrors for repositories in a Bitbucket workspace."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("bitbucket"))
