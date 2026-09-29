"""Shared API and orchestration code for pull mirrors from Git hosts.

Repository discovery finishes before any mirror is created. This prevents a
failed pagination request from producing an apparently successful partial run.
API errors deliberately omit response bodies, which may contain credentials.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class MirrorError(Exception):
    """A configuration, network, or API failure safe to show an operator."""


class NoRedirect(HTTPRedirectHandler):
    """Reject redirects rather than forwarding authorization to a new URL."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(NoRedirect)


@dataclass(frozen=True)
class Repository:
    name: str
    clone_url: str
    fork: bool = False
    archived: bool = False


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MirrorError(f"Missing required environment variable: {name}")
    return value


def https_base_url(value: str, label: str) -> str:
    """Accept a hostname or HTTPS URL, including installations under a path."""
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise MirrorError(f"{label} must be an HTTPS hostname or URL without credentials")
    if parsed.query or parsed.fragment:
        raise MirrorError(f"{label} must not include a query or fragment")
    return value.rstrip("/")


def gitea_base_url(value: str) -> str:
    """Validate the destination URL used by the mirror and delete commands."""
    return https_base_url(value, "GITEA_URL")


def basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return "Basic " + encoded


def request_json(url: str, headers: dict[str, str], *, method: str = "GET",
                 payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> tuple[object, dict]:
    """Send one JSON request; keep tokens and response bodies out of errors."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=30) as response:
            status = response.status
            response_headers = dict(response.headers)
            body = response.read()
    except HTTPError as exc:
        if exc.code == 404 and 404 in expected:
            return None, {}
        raise MirrorError(f"{method} {urlsplit(url).hostname}: HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise MirrorError(f"{method} {urlsplit(url).hostname}: connection failed ({type(exc).__name__})") from None
    if status not in expected:
        raise MirrorError(f"{method} {urlsplit(url).hostname}: HTTP {status}")
    if not body:
        return None, response_headers
    try:
        return json.loads(body), response_headers
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MirrorError(f"{method} {urlsplit(url).hostname}: invalid JSON response") from None


def same_origin_page(next_url: str, current_url: str) -> str:
    """Restrict pagination links so credentials cannot be sent to another host."""
    resolved = urljoin(current_url, next_url)
    first, next_page = urlsplit(current_url), urlsplit(resolved)
    if next_page.scheme != "https" or next_page.netloc != first.netloc:
        raise MirrorError("API pagination link points to a different origin")
    return resolved


def https_clone_url(value: object, source: str) -> str:
    """Reject malformed or credential-bearing clone addresses from API data."""
    if not isinstance(value, str):
        raise MirrorError(f"{source} returned an invalid HTTPS clone URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise MirrorError(f"{source} returned an invalid HTTPS clone URL")
    return value


def next_link(headers: dict, current_url: str) -> str:
    """Follow an RFC 8288 ``rel=next`` link without forwarding credentials."""
    link_header = next((value for key, value in headers.items() if key.lower() == "link"), "")
    for part in link_header.split(","):
        match = re.search(r'<([^>]+)>\s*;[^,]*\brel="?next"?', part)
        if match:
            return same_origin_page(match.group(1), current_url)
    return ""


def unique_names(repositories: list[Repository]) -> None:
    """Stop before migration if two source repositories map to one Gitea name."""
    seen: set[str] = set()
    for repo in repositories:
        key = repo.name.casefold()
        if key in seen:
            raise MirrorError(f"Multiple source repositories map to {repo.name!r}")
        seen.add(key)


def github_repositories(username: str, token: str, target: str) -> list[Repository]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "User-Agent": "git-backups"}
    if target.casefold() == username.casefold():
        url = "https://api.github.com/user/repos?affiliation=owner&per_page=100"
    else:
        # The user endpoint exposes public repositories only.
        url = f"https://api.github.com/users/{quote(target, safe='')}/repos?type=owner&per_page=100"
    repositories: list[Repository] = []
    seen_pages: set[str] = set()
    while url:
        if url in seen_pages:
            raise MirrorError("GitHub pagination contains a repeated page")
        seen_pages.add(url)
        data, response_headers = request_json(url, headers)
        if not isinstance(data, list):
            raise MirrorError("GitHub returned an unexpected repository list")
        for item in data:
            if not isinstance(item, dict) or not item.get("name") or not item.get("clone_url"):
                raise MirrorError("GitHub returned an incomplete repository")
            repositories.append(Repository(item["name"], https_clone_url(item["clone_url"], "GitHub"),
                                           bool(item.get("fork")), bool(item.get("archived"))))
        url = next_link(response_headers, url)
    return repositories


def forge_repositories(base_url: str, username: str, token: str,
                       owner: str, kind: str) -> list[Repository]:
    """List repositories from Codeberg, Forgejo, or a Gitea source instance."""
    if kind not in {"user", "org"}:
        raise MirrorError("Source owner kind must be 'user' or 'org'")
    headers = {"Authorization": f"token {token}", "Accept": "application/json",
               "User-Agent": "git-backups"}
    if kind == "org":
        endpoint = f"/api/v1/orgs/{quote(owner, safe='')}/repos"
    elif owner.casefold() == username.casefold():
        endpoint = "/api/v1/user/repos"
    else:
        endpoint = f"/api/v1/users/{quote(owner, safe='')}/repos"
    url = base_url + endpoint + "?limit=100&page=1"
    repositories: list[Repository] = []
    seen_pages: set[str] = set()
    while url:
        if url in seen_pages:
            raise MirrorError("Forge API pagination contains a repeated page")
        seen_pages.add(url)
        data, response_headers = request_json(url, headers)
        if not isinstance(data, list):
            raise MirrorError("Forge API returned an unexpected repository list")
        for item in data:
            if not isinstance(item, dict) or not item.get("name"):
                raise MirrorError("Forge API returned an incomplete repository")
            repositories.append(Repository(item["name"],
                                           https_clone_url(item.get("clone_url"), "Forge API"),
                                           bool(item.get("fork")), bool(item.get("archived"))))
        url = next_link(response_headers, url)
    return repositories


def gitlab_repositories(base_url: str, token: str, group_path: str | None) -> list[Repository]:
    """List owned projects or projects in one GitLab group and its subgroups."""
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json",
               "User-Agent": "git-backups"}
    if group_path:
        endpoint = f"/api/v4/groups/{quote(group_path, safe='')}/projects"
        query = "?include_subgroups=true&with_shared=false&per_page=100"
    else:
        endpoint = "/api/v4/projects"
        query = "?owned=true&per_page=100"
    url = base_url + endpoint + query
    repositories: list[Repository] = []
    seen_pages: set[str] = set()
    while url:
        if url in seen_pages:
            raise MirrorError("GitLab pagination contains a repeated page")
        seen_pages.add(url)
        data, response_headers = request_json(url, headers)
        if not isinstance(data, list):
            raise MirrorError("GitLab returned an unexpected project list")
        for item in data:
            if not isinstance(item, dict) or not item.get("path_with_namespace"):
                raise MirrorError("GitLab returned an incomplete project")
            # GitLab namespaces may contain the same project path. Flattening
            # the full path preserves a readable, deterministic Gitea name.
            name = item["path_with_namespace"].replace("/", "--")
            repositories.append(Repository(name,
                                           https_clone_url(item.get("http_url_to_repo"), "GitLab"),
                                           bool(item.get("forked_from_project")),
                                           bool(item.get("archived"))))
        url = next_link(response_headers, url)
    return repositories


def azure_repositories(organization: str, token: str) -> list[Repository]:
    """List Azure Repos Git repositories across the accessible organization."""
    headers = {"Authorization": basic_auth("", token), "Accept": "application/json",
               "User-Agent": "git-backups"}
    url = f"https://dev.azure.com/{quote(organization, safe='')}/_apis/git/repositories?api-version=7.1"
    data, _ = request_json(url, headers)
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        raise MirrorError("Azure DevOps returned an unexpected repository list")
    repositories: list[Repository] = []
    for item in data["value"]:
        if not isinstance(item, dict) or not isinstance(item.get("project"), dict):
            raise MirrorError("Azure DevOps returned an incomplete repository")
        project = item["project"].get("name")
        name = item.get("name")
        if not isinstance(project, str) or not isinstance(name, str):
            raise MirrorError("Azure DevOps repository lacks a project or name")
        # Azure allows duplicate repository names in different projects.
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{project}--{name}").strip("-.")
        if not safe_name:
            raise MirrorError("Azure DevOps repository produced an empty Gitea name")
        repositories.append(Repository(safe_name,
                                       https_clone_url(item.get("remoteUrl"), "Azure DevOps")))
    return repositories


def manifest_repositories(path: str) -> list[Repository]:
    """Load explicit HTTPS repositories from a local JSON manifest."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"Cannot read repository manifest ({type(exc).__name__})") from None
    if not isinstance(data, list):
        raise MirrorError("Repository manifest must contain a JSON array")
    repositories: list[Repository] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise MirrorError("Repository manifest contains an invalid name")
        repositories.append(Repository(item["name"],
                                       https_clone_url(item.get("clone_url"), "Manifest")))
    return repositories


def bitbucket_repositories(username: str, password: str, workspace: str) -> list[Repository]:
    headers = {"Authorization": basic_auth(username, password), "Accept": "application/json",
               "User-Agent": "git-backups"}
    url = f"https://api.bitbucket.org/2.0/repositories/{quote(workspace, safe='')}?pagelen=100"
    repositories: list[Repository] = []
    seen_pages: set[str] = set()
    while url:
        if url in seen_pages:
            raise MirrorError("Bitbucket pagination contains a repeated page")
        seen_pages.add(url)
        data, _ = request_json(url, headers)
        if not isinstance(data, dict) or not isinstance(data.get("values"), list):
            raise MirrorError("Bitbucket returned an unexpected repository list")
        for item in data["values"]:
            if not isinstance(item, dict):
                raise MirrorError("Bitbucket returned an incomplete repository")
            # The slug is the stable Git name; display names can contain spaces.
            clone_links = item.get("links", {}).get("clone", [])
            clone_url = next((link.get("href") for link in clone_links
                              if link.get("name") == "https"), None)
            if not item.get("slug") or not clone_url:
                raise MirrorError("Bitbucket repository lacks a slug or HTTPS clone URL")
            repositories.append(Repository(item["slug"], https_clone_url(clone_url, "Bitbucket"),
                                           fork=bool(item.get("parent"))))
        next_page = data.get("next")
        if next_page is not None and not isinstance(next_page, str):
            raise MirrorError("Bitbucket returned an invalid pagination link")
        url = same_origin_page(next_page, url) if next_page else ""
    return repositories


def select_repositories(repositories: list[Repository], *, pattern: str,
                        exclude_forks: bool, exclude_archived: bool) -> list[Repository]:
    return [repo for repo in repositories
            if fnmatch.fnmatchcase(repo.name, pattern)
            and not (exclude_forks and repo.fork)
            and not (exclude_archived and repo.archived)]


def mirror_repositories(repositories: list[Repository], *, base_url: str, token: str,
                        owner: str, source_username: str, source_password: str,
                        interval: str, dry_run: bool) -> tuple[int, int, int]:
    headers = {"Authorization": f"token {token}", "Accept": "application/json",
               "Content-Type": "application/json", "User-Agent": "git-backups"}
    created = skipped = failed = 0
    for repo in repositories:
        path = f"/api/v1/repos/{quote(owner, safe='')}/{quote(repo.name, safe='')}"
        try:
            existing, _ = request_json(base_url + path, headers, expected=(200, 404))
            if existing is not None:
                print(f"SKIP {owner}/{repo.name}: already exists")
                skipped += 1
                continue
            if dry_run:
                print(f"PLAN {owner}/{repo.name}")
                skipped += 1
                continue
            # Gitea receives source credentials in the migration body. They
            # must never be embedded in clone URLs or printed in logs.
            payload = {"clone_addr": repo.clone_url, "repo_name": repo.name,
                       "repo_owner": owner, "mirror": True, "private": True,
                       "service": "git", "mirror_interval": interval,
                       }
            if source_password:
                payload.update({"auth_username": source_username,
                                "auth_password": source_password})
            request_json(base_url + "/api/v1/repos/migrate", headers, method="POST",
                         payload=payload, expected=(201,))
            print(f"CREATE {owner}/{repo.name}")
            created += 1
        except MirrorError as exc:
            print(f"ERROR {owner}/{repo.name}: {exc}", file=sys.stderr)
            failed += 1
    return created, skipped, failed


def main(source: str) -> int:
    if source not in {"github", "bitbucket", "codeberg", "forgejo", "gitea",
                      "gitlab", "azure", "manifest"}:
        raise ValueError(f"Unsupported source adapter: {source}")
    parser = argparse.ArgumentParser(description=f"Mirror {source} repositories to Gitea")
    parser.add_argument("--dry-run", action="store_true", help="list proposed mirrors without creating them")
    parser.add_argument("--include", default="*", metavar="GLOB", help="repository name glob (default: *)")
    if source not in {"azure", "manifest"}:
        parser.add_argument("--exclude-forks", action="store_true",
                            help="skip forked repositories")
    if source in {"github", "codeberg", "forgejo", "gitea", "gitlab"}:
        parser.add_argument("--exclude-archived", action="store_true",
                            help="skip archived repositories")
    if source == "manifest":
        parser.add_argument("--manifest", required=True, help="JSON file with name and clone_url entries")
    parser.add_argument("--mirror-interval", default="24h", metavar="DURATION",
                        help="Gitea mirror interval (default: 24h)")
    args = parser.parse_args()
    try:
        base_url = gitea_base_url(required_env("GITEA_URL"))
        gitea_token = required_env("GITEA_TOKEN")
        owner = required_env("GITEA_ORG_NAME")
        if not re.fullmatch(r"[1-9][0-9]*[smh]", args.mirror_interval):
            raise MirrorError("--mirror-interval must look like 30m or 12h")
        if source == "github":
            username = required_env("GITHUB_USERNAME")
            password = required_env("GITHUB_TOKEN")
            target = required_env("GITHUB_TARGET_USERNAME")
            repositories = github_repositories(username, password, target)
        elif source == "bitbucket":
            username = required_env("BITBUCKET_USERNAME")
            password = required_env("BITBUCKET_APP_PASSWORD")
            target = required_env("BITBUCKET_TARGET_USERNAME")
            repositories = bitbucket_repositories(username, password, target)
        elif source in {"codeberg", "forgejo", "gitea"}:
            prefix = {"codeberg": "CODEBERG", "forgejo": "FORGEJO",
                      "gitea": "SOURCE_GITEA"}[source]
            server = ("https://codeberg.org" if source == "codeberg" else
                      https_base_url(required_env(prefix + "_URL"), prefix + "_URL"))
            username = required_env(prefix + "_USERNAME")
            password = required_env(prefix + "_TOKEN")
            target = os.environ.get(prefix + "_TARGET_OWNER", username).strip() or username
            kind = os.environ.get(prefix + "_OWNER_KIND", "user").strip().lower() or "user"
            repositories = forge_repositories(server, username, password, target, kind)
        elif source == "gitlab":
            server = https_base_url(required_env("GITLAB_URL"), "GITLAB_URL")
            password = required_env("GITLAB_TOKEN")
            username = "oauth2"
            group = os.environ.get("GITLAB_GROUP_PATH", "").strip() or None
            repositories = gitlab_repositories(server, password, group)
        elif source == "azure":
            organization = required_env("AZURE_DEVOPS_ORGANIZATION")
            password = required_env("AZURE_DEVOPS_PAT")
            username = "git"
            repositories = azure_repositories(organization, password)
        else:
            username = os.environ.get("MIRROR_SOURCE_USERNAME", "").strip()
            password = os.environ.get("MIRROR_SOURCE_TOKEN", "").strip()
            if password and not username:
                raise MirrorError("MIRROR_SOURCE_USERNAME is required with MIRROR_SOURCE_TOKEN")
            repositories = manifest_repositories(args.manifest)
        selected = select_repositories(repositories, pattern=args.include,
                                       exclude_forks=getattr(args, "exclude_forks", False),
                                       exclude_archived=getattr(args, "exclude_archived", False))
        unique_names(selected)
        created, skipped, failed = mirror_repositories(selected, base_url=base_url,
                                                        token=gitea_token, owner=owner,
                                                        source_username=username,
                                                        source_password=password,
                                                        interval=args.mirror_interval,
                                                        dry_run=args.dry_run)
        print(f"Summary: discovered={len(repositories)} selected={len(selected)} "
              f"created={created} skipped={skipped} failed={failed}")
        return 1 if failed else 0
    except MirrorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
