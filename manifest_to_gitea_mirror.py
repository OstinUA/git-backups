#!/usr/bin/env python3
"""Create Gitea pull mirrors from an explicit JSON repository manifest."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("manifest"))
