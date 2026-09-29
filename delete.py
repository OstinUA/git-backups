#!/usr/bin/env python3
"""Delete one named Gitea repository after an explicit command line confirmation.

This maintenance command never enumerates repositories. A plain invocation is
read only, and deletion requires both --execute and the exact owner name.
"""

from __future__ import annotations

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from mirror import MirrorError, _opener, gitea_base_url, required_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete one named Gitea repository")
    parser.add_argument("--repo", required=True, help="exact repository name")
    parser.add_argument("--execute", action="store_true", help="perform the deletion")
    parser.add_argument("--confirm-owner", help="repeat GITEA_ORG_NAME to confirm deletion")
    args = parser.parse_args()
    try:
        owner = required_env("GITEA_ORG_NAME")
        base_url = gitea_base_url(required_env("GITEA_URL"))
        token = required_env("GITEA_TOKEN")
        label = f"{owner}/{args.repo}"
        if not args.execute:
            print(f"PLAN DELETE {label}; add --execute --confirm-owner {owner} to proceed")
            return 0
        if args.confirm_owner != owner:
            raise MirrorError("--confirm-owner must exactly match GITEA_ORG_NAME")
        url = f"{base_url}/api/v1/repos/{quote(owner, safe='')}/{quote(args.repo, safe='')}"
        request = Request(url, headers={"Authorization": f"token {token}"}, method="DELETE")
        try:
            with _opener.open(request, timeout=30) as response:
                if response.status != 204:
                    raise MirrorError(f"Gitea deletion returned HTTP {response.status}")
        except HTTPError as exc:
            raise MirrorError(f"Gitea deletion returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise MirrorError(f"Gitea connection failed ({type(exc).__name__})") from None
        print(f"DELETED {label}")
        return 0
    except MirrorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
