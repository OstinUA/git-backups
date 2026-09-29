#!/usr/bin/env python3
"""Create Gitea pull mirrors from GitLab.com or self-managed GitLab."""

from mirror import main


if __name__ == "__main__":
    raise SystemExit(main("gitlab"))
