#!/usr/bin/env python3
"""Delete one named Gitea repository after an explicit command line confirmation.

This maintenance command never enumerates repositories. A plain invocation is
read only, and deletion requires both --execute and the exact owner name.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from mirror import (MirrorError, gitea_base_url, gitea_repository_url, request_json,
                    required_env)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete one named Gitea repository")
    parser.add_argument("--repo", required=True, help="exact repository name")
    parser.add_argument("--execute", action="store_true", help="perform the deletion")
    parser.add_argument("--confirm-owner", help="repeat GITEA_ORG_NAME to confirm deletion")
    args = parser.parse_args(argv)
    try:
        owner = required_env("GITEA_ORG_NAME")
        base_url = gitea_base_url(required_env("GITEA_URL"))
        token = required_env("GITEA_TOKEN")
        url = gitea_repository_url(base_url, owner, args.repo)
        label = f"{owner}/{args.repo}"
        if not args.execute:
            print(f"PLAN DELETE {label}; add --execute --confirm-owner {owner} to proceed")
            return 0
        if args.confirm_owner != owner:
            raise MirrorError("--confirm-owner must exactly match GITEA_ORG_NAME")
        request_json(url, {"Authorization": f"token {token}"}, method="DELETE",
                     expected=(204,))
    except MirrorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"DELETED {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
