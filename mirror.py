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
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from http.client import HTTPException
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, quote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

USER_AGENT = "git-backups"
REQUEST_TIMEOUT = 30
# Gitea answers a migration request only after it has cloned the source, so
# this call needs far more headroom than an ordinary API round trip.
MIGRATION_TIMEOUT = 600
# Generous for any repository listing, yet small enough that a misbehaving
# server cannot exhaust the runner's memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
EXISTENCE_CHECK_WORKERS = 8

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_MIRROR_INTERVAL = re.compile(r"[1-9][0-9]*[smh]")
_LINK_VALUE = re.compile(r"<([^>]*)>([^<]*)")
_LINK_REL = re.compile(r""";\s*rel\s*=\s*(?:"([^"]*)"|([^\s;,"]+))""", re.IGNORECASE)

Headers = Mapping[str, str]


class MirrorError(Exception):
    """A configuration, network, or API failure safe to show an operator."""


class HTTPStatusError(MirrorError):
    """An API response whose status code the caller did not expect."""

    def __init__(self, target: str, status: int) -> None:
        super().__init__(f"{target}: HTTP {status}")
        self.status = status


@dataclass(frozen=True, slots=True)
class Repository:
    """A source repository and the name its mirror receives in Gitea."""

    name: str
    clone_url: str
    fork: bool = False
    archived: bool = False


class MirrorCounts(NamedTuple):
    """Outcome of one run; planned mirrors of a dry run count as skipped."""

    created: int
    skipped: int
    failed: int


# --- Configuration ----------------------------------------------------------

def optional_env(name: str, default: str = "") -> str:
    """Read a setting from the environment, treating a blank value as unset."""
    value = os.environ.get(name, "").strip()
    # A stray line break in a secret would otherwise surface in http.client,
    # whose error message quotes the rejected header value, token included.
    if _CONTROL_CHARACTERS.search(value):
        raise MirrorError(f"Environment variable {name} contains control characters")
    return value or default


def required_env(name: str) -> str:
    value = optional_env(name)
    if not value:
        raise MirrorError(f"Missing required environment variable: {name}")
    return value


def _https_parts(value: str) -> SplitResult | None:
    """Split a credential-free HTTPS URL, or return ``None`` if it is not one."""
    # urlsplit() silently drops tabs and line breaks, so the URL it validates
    # would differ from the string that is later sent over the wire.
    if _CONTROL_CHARACTERS.search(value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:  # Unbalanced IPv6 brackets or a non-numeric port.
        return None
    if parts.scheme != "https" or not parts.hostname or port == 0 or "@" in parts.netloc:
        return None
    return parts


def https_base_url(value: str, label: str) -> str:
    """Accept a hostname or HTTPS URL, including installations under a path."""
    if "://" not in value:
        value = "https://" + value
    if _https_parts(value) is None or " " in value:
        raise MirrorError(f"{label} must be an HTTPS hostname or URL without credentials")
    if "?" in value or "#" in value:
        raise MirrorError(f"{label} must not include a query or fragment")
    return value.rstrip("/")


def gitea_base_url(value: str) -> str:
    """Validate the destination URL used by the mirror and delete commands."""
    return https_base_url(value, "GITEA_URL")


def https_clone_url(value: object, source: str) -> str:
    """Reject malformed or credential-bearing clone addresses from API data."""
    if isinstance(value, str):
        parts = _https_parts(value)
        if parts is not None and not parts.query and not parts.fragment:
            return value
    raise MirrorError(f"{source} returned an invalid HTTPS clone URL")


def _without_account_name(value: object) -> object:
    """Drop the account name that some hosts prepend to HTTPS clone URLs.

    Bitbucket and Azure DevOps may report ``https://account@host/...``. The
    name is not a secret, and Gitea receives the credentials separately, so
    the address is reduced to its anonymous form. A URL that also carries a
    password is returned untouched for ``https_clone_url`` to reject.
    """
    if not isinstance(value, str):
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.username is None or parts.password is not None:
        return value
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))


# --- HTTP transport ---------------------------------------------------------

class NoRedirect(HTTPRedirectHandler):
    """Reject redirects rather than forwarding authorization to a new URL."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


_opener = build_opener(NoRedirect)


def basic_auth(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return "Basic " + encoded


def _fold_headers(fields: Any) -> dict[str, str]:
    """Lower-case header names and join repeated fields as RFC 9110 permits."""
    folded: dict[str, str] = {}
    for name, value in fields.items():
        key = name.lower()
        folded[key] = f"{folded[key]}, {value}" if key in folded else value
    return folded


def _failure_name(exc: Exception) -> str:
    """Name a transport failure without repeating its message."""
    if isinstance(exc, URLError) and isinstance(exc.reason, Exception):
        exc = exc.reason
    return type(exc).__name__


def request_json(url: str, headers: Headers, *, method: str = "GET",
                 payload: Mapping[str, Any] | None = None,
                 expected: tuple[int, ...] = (200,),
                 timeout: float = REQUEST_TIMEOUT) -> tuple[Any, dict[str, str]]:
    """Send one JSON request; keep tokens and response bodies out of errors.

    Returns the decoded body, or ``None`` when it is empty, together with the
    response headers keyed by lower-case name. A status outside ``expected``
    raises ``HTTPStatusError``.
    """
    address = urlsplit(url)
    target = f"{method} {address.hostname}"
    # Callers validate their URLs; this guards the single point of egress,
    # because the stock opener would also serve file: and ftp: addresses.
    if address.scheme != "https":
        raise MirrorError(f"{target}: refusing a request that is not HTTPS")
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    try:
        request = Request(url, data=data, headers=request_headers, method=method)
        with _opener.open(request, timeout=timeout) as response:
            status = response.status
            response_headers = _fold_headers(response.headers)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        raise HTTPStatusError(target, exc.code) from None
    except (OSError, HTTPException, ValueError) as exc:
        # http.client reports malformed header values with a ValueError that
        # quotes them, so only the exception type is allowed into the log.
        raise MirrorError(f"{target}: connection failed ({_failure_name(exc)})") from None
    if status not in expected:
        raise HTTPStatusError(target, status)
    if len(body) > MAX_RESPONSE_BYTES:
        raise MirrorError(f"{target}: response is larger than {MAX_RESPONSE_BYTES} bytes")
    if not body:
        return None, response_headers
    try:
        return json.loads(body), response_headers
    except (ValueError, RecursionError):
        raise MirrorError(f"{target}: invalid JSON response") from None


# --- Pagination -------------------------------------------------------------

def same_origin_page(next_url: str, current_url: str) -> str:
    """Restrict pagination links so credentials cannot be sent to another host."""
    try:
        resolved = urljoin(current_url, next_url)
        current, following = urlsplit(current_url), urlsplit(resolved)
    except ValueError:
        raise MirrorError("API pagination link is malformed") from None
    if following.scheme != "https" or following.netloc != current.netloc:
        raise MirrorError("API pagination link points to a different origin")
    return resolved


def next_link(headers: Headers, current_url: str) -> str:
    """Follow an RFC 8288 ``rel=next`` link without forwarding credentials.

    ``headers`` uses lower-case names, as returned by ``request_json``.
    """
    for target, parameters in _LINK_VALUE.findall(headers.get("link", "")):
        rel = _LINK_REL.search(parameters)
        if rel and "next" in (rel.group(1) or rel.group(2) or "").lower().split():
            return same_origin_page(target, current_url)
    return ""


def _link_header_page(_page: Any, headers: Headers, current_url: str) -> str:
    return next_link(headers, current_url)


def _pages(url: str, headers: Headers, source: str,
           next_page: Callable[[Any, Headers, str], str] = _link_header_page) -> Iterator[Any]:
    """Yield the decoded body of every page of a listing.

    ``next_page`` receives the page, its response headers, and its URL, and
    returns the URL of the following page or an empty string after the last.
    """
    seen: set[str] = set()
    while url:
        if url in seen:
            raise MirrorError(f"{source} pagination contains a repeated page")
        seen.add(url)
        page, response_headers = request_json(url, headers)
        yield page
        url = next_page(page, response_headers, url)


# --- Source discovery -------------------------------------------------------

def _records(page: object, source: str) -> list[dict[str, Any]]:
    """Return the repository objects of one listing after a shape check."""
    if not isinstance(page, list) or not all(isinstance(record, dict) for record in page):
        raise MirrorError(f"{source} returned an unexpected repository list")
    return page


def _text(record: Mapping[str, Any], key: str, source: str) -> str:
    """Return a mandatory string field; API data is not trusted to be typed."""
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise MirrorError(f"{source} returned a repository without a valid {key!r} field")
    return value


def _listed_repositories(url: str, headers: Headers, source: str,
                         parse: Callable[[dict[str, Any]], Repository]) -> list[Repository]:
    """Collect every repository of a listing paginated through ``Link`` headers."""
    return [parse(record)
            for page in _pages(url, headers, source)
            for record in _records(page, source)]


def github_repositories(username: str, token: str, target: str) -> list[Repository]:
    """List repositories owned by a GitHub account."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    if target.casefold() == username.casefold():
        url = "https://api.github.com/user/repos?affiliation=owner&per_page=100"
    else:
        # The user endpoint exposes public repositories only.
        url = (f"https://api.github.com/users/{quote(target, safe='')}/repos"
               "?type=owner&per_page=100")

    def parse(record: dict[str, Any]) -> Repository:
        return Repository(_text(record, "name", "GitHub"),
                          https_clone_url(record.get("clone_url"), "GitHub"),
                          fork=bool(record.get("fork")),
                          archived=bool(record.get("archived")))

    return _listed_repositories(url, headers, "GitHub", parse)


def forge_repositories(base_url: str, username: str, token: str,
                       owner: str, kind: str) -> list[Repository]:
    """List repositories from Codeberg, Forgejo, or a Gitea source instance."""
    if kind not in {"user", "org"}:
        raise MirrorError("Source owner kind must be 'user' or 'org'")
    if kind == "org":
        endpoint = f"orgs/{quote(owner, safe='')}/repos"
    elif owner.casefold() == username.casefold():
        # Only the authenticated-user endpoint includes private repositories.
        endpoint = "user/repos"
    else:
        endpoint = f"users/{quote(owner, safe='')}/repos"
    url = f"{base_url}/api/v1/{endpoint}?limit=100&page=1"

    def parse(record: dict[str, Any]) -> Repository:
        return Repository(_text(record, "name", "Forge API"),
                          https_clone_url(record.get("clone_url"), "Forge API"),
                          fork=bool(record.get("fork")),
                          archived=bool(record.get("archived")))

    return _listed_repositories(url, {"Authorization": f"token {token}"}, "Forge API", parse)


def gitlab_repositories(base_url: str, token: str, group_path: str | None) -> list[Repository]:
    """List owned projects or projects in one GitLab group and its subgroups."""
    if group_path:
        url = (f"{base_url}/api/v4/groups/{quote(group_path, safe='')}/projects"
               "?include_subgroups=true&with_shared=false&per_page=100")
    else:
        url = f"{base_url}/api/v4/projects?owned=true&per_page=100"

    def parse(record: dict[str, Any]) -> Repository:
        # GitLab namespaces may contain the same project path. Flattening
        # the full path preserves a readable, deterministic Gitea name.
        name = _text(record, "path_with_namespace", "GitLab").replace("/", "--")
        return Repository(name,
                          https_clone_url(record.get("http_url_to_repo"), "GitLab"),
                          fork=bool(record.get("forked_from_project")),
                          archived=bool(record.get("archived")))

    return _listed_repositories(url, {"PRIVATE-TOKEN": token}, "GitLab", parse)


def _bitbucket_next_page(page: Any, _headers: Headers, current_url: str) -> str:
    """Bitbucket announces the next page in the response body."""
    following = page.get("next")
    if following is not None and not isinstance(following, str):
        raise MirrorError("Bitbucket returned an invalid pagination link")
    return same_origin_page(following, current_url) if following else ""


def _bitbucket_https_link(record: Mapping[str, Any]) -> object:
    links = record.get("links")
    clone_links = links.get("clone") if isinstance(links, dict) else None
    for link in clone_links if isinstance(clone_links, list) else ():
        if isinstance(link, dict) and link.get("name") == "https":
            return link.get("href")
    return None


def bitbucket_repositories(username: str, password: str, workspace: str) -> list[Repository]:
    """List repositories in one Bitbucket Cloud workspace."""
    headers = {"Authorization": basic_auth(username, password)}
    url = f"https://api.bitbucket.org/2.0/repositories/{quote(workspace, safe='')}?pagelen=100"
    repositories: list[Repository] = []
    for page in _pages(url, headers, "Bitbucket", _bitbucket_next_page):
        if not isinstance(page, dict):
            raise MirrorError("Bitbucket returned an unexpected repository list")
        for record in _records(page.get("values"), "Bitbucket"):
            # The slug is the stable Git name; display names can contain spaces.
            name = _text(record, "slug", "Bitbucket")
            clone_url = _without_account_name(_bitbucket_https_link(record))
            repositories.append(Repository(name, https_clone_url(clone_url, "Bitbucket"),
                                           fork=bool(record.get("parent"))))
    return repositories


def azure_repositories(organization: str, token: str) -> list[Repository]:
    """List Azure Repos Git repositories across the accessible organization."""
    headers = {"Authorization": basic_auth("", token)}
    url = (f"https://dev.azure.com/{quote(organization, safe='')}"
           "/_apis/git/repositories?api-version=7.1")
    data, _ = request_json(url, headers)
    if not isinstance(data, dict):
        raise MirrorError("Azure DevOps returned an unexpected repository list")
    repositories: list[Repository] = []
    for record in _records(data.get("value"), "Azure DevOps"):
        project = record.get("project")
        if not isinstance(project, dict):
            raise MirrorError("Azure DevOps returned a repository without a project")
        # Azure allows duplicate repository names in different projects.
        qualified = "--".join((_text(project, "name", "Azure DevOps"),
                               _text(record, "name", "Azure DevOps")))
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", qualified).strip("-.")
        if not name:
            raise MirrorError("Azure DevOps repository produced an empty Gitea name")
        clone_url = _without_account_name(record.get("remoteUrl"))
        repositories.append(Repository(name, https_clone_url(clone_url, "Azure DevOps")))
    return repositories


def manifest_repositories(path: str) -> list[Repository]:
    """Load explicit HTTPS repositories from a local JSON manifest."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
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


# --- Selection --------------------------------------------------------------

def select_repositories(repositories: list[Repository], *, pattern: str,
                        exclude_forks: bool, exclude_archived: bool) -> list[Repository]:
    return [repo for repo in repositories
            if fnmatch.fnmatchcase(repo.name, pattern)
            and not (exclude_forks and repo.fork)
            and not (exclude_archived and repo.archived)]


def unique_names(repositories: list[Repository]) -> None:
    """Stop before migration if two source repositories map to one Gitea name."""
    seen: set[str] = set()
    for repo in repositories:
        key = repo.name.casefold()
        if key in seen:
            raise MirrorError(f"Multiple source repositories map to {repo.name!r}")
        seen.add(key)


# --- Destination ------------------------------------------------------------

def gitea_repository_url(base_url: str, owner: str, name: str) -> str:
    """Build the API address of one destination repository."""
    for segment in (owner, name):
        # Percent-encoding leaves dot segments intact, and a server that
        # normalises them would route the request to a different resource.
        if segment in {".", ".."}:
            raise MirrorError(f"{segment!r} is not a valid Gitea owner or repository name")
    return f"{base_url}/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _repository_exists(base_url: str, owner: str, name: str, headers: Headers) -> bool:
    try:
        request_json(gitea_repository_url(base_url, owner, name), headers)
    except HTTPStatusError as exc:
        if exc.status == 404:
            return False
        raise
    return True


def _log(message: str, *, error: bool = False) -> None:
    """Emit one progress line at once so piped CI output stays in order."""
    print(message, file=sys.stderr if error else sys.stdout, flush=True)


def mirror_repositories(repositories: list[Repository], *, base_url: str, token: str,
                        owner: str, source_username: str, source_password: str,
                        interval: str, dry_run: bool) -> MirrorCounts:
    """Create a pull mirror for every repository missing from the destination."""
    headers = {"Authorization": f"token {token}"}
    created = skipped = failed = 0
    # Existence checks are independent read-only requests, so they run ahead
    # of the loop. Migrations stay sequential to bound the load on Gitea and
    # to keep the log in repository order.
    pool = ThreadPoolExecutor(max_workers=EXISTENCE_CHECK_WORKERS)
    try:
        checks = [pool.submit(_repository_exists, base_url, owner, repo.name, headers)
                  for repo in repositories]
        for repo, check in zip(repositories, checks, strict=True):
            label = f"{owner}/{repo.name}"
            try:
                if check.result():
                    _log(f"SKIP {label}: already exists")
                    skipped += 1
                    continue
                if dry_run:
                    _log(f"PLAN {label}")
                    skipped += 1
                    continue
                # Gitea receives source credentials in the migration body. They
                # must never be embedded in clone URLs or printed in logs.
                payload = {"clone_addr": repo.clone_url, "repo_name": repo.name,
                           "repo_owner": owner, "mirror": True, "private": True,
                           "service": "git", "mirror_interval": interval}
                if source_password:
                    payload.update({"auth_username": source_username,
                                    "auth_password": source_password})
                request_json(f"{base_url}/api/v1/repos/migrate", headers, method="POST",
                             payload=payload, expected=(201,), timeout=MIGRATION_TIMEOUT)
                _log(f"CREATE {label}")
                created += 1
            except MirrorError as exc:
                _log(f"ERROR {label}: {exc}", error=True)
                failed += 1
    finally:
        # Every check has been consumed by now unless the run is being
        # aborted, in which case the queued ones are no longer wanted.
        pool.shutdown(wait=False, cancel_futures=True)
    return MirrorCounts(created, skipped, failed)


# --- Command line -----------------------------------------------------------

class Discovery(NamedTuple):
    """Repositories found at a source and the credentials Gitea pulls them with."""

    repositories: list[Repository]
    username: str
    password: str


def _discover_github(_args: argparse.Namespace) -> Discovery:
    username = required_env("GITHUB_USERNAME")
    token = required_env("GITHUB_TOKEN")
    target = required_env("GITHUB_TARGET_USERNAME")
    return Discovery(github_repositories(username, token, target), username, token)


def _discover_bitbucket(_args: argparse.Namespace) -> Discovery:
    username = required_env("BITBUCKET_USERNAME")
    password = required_env("BITBUCKET_APP_PASSWORD")
    workspace = required_env("BITBUCKET_TARGET_USERNAME")
    return Discovery(bitbucket_repositories(username, password, workspace), username, password)


def _discover_forge(prefix: str, server: str | None, _args: argparse.Namespace) -> Discovery:
    """Discover from a Gitea-compatible API configured through ``<prefix>_*``."""
    if server is None:
        server = https_base_url(required_env(f"{prefix}_URL"), f"{prefix}_URL")
    username = required_env(f"{prefix}_USERNAME")
    token = required_env(f"{prefix}_TOKEN")
    owner = optional_env(f"{prefix}_TARGET_OWNER", username)
    kind = optional_env(f"{prefix}_OWNER_KIND", "user").lower()
    return Discovery(forge_repositories(server, username, token, owner, kind), username, token)


def _discover_gitlab(_args: argparse.Namespace) -> Discovery:
    server = https_base_url(required_env("GITLAB_URL"), "GITLAB_URL")
    token = required_env("GITLAB_TOKEN")
    group = optional_env("GITLAB_GROUP_PATH") or None
    # GitLab accepts any non-empty username alongside a token over HTTPS.
    return Discovery(gitlab_repositories(server, token, group), "oauth2", token)


def _discover_azure(_args: argparse.Namespace) -> Discovery:
    organization = required_env("AZURE_DEVOPS_ORGANIZATION")
    token = required_env("AZURE_DEVOPS_PAT")
    return Discovery(azure_repositories(organization, token), "git", token)


def _discover_manifest(args: argparse.Namespace) -> Discovery:
    username = optional_env("MIRROR_SOURCE_USERNAME")
    token = optional_env("MIRROR_SOURCE_TOKEN")
    if token and not username:
        raise MirrorError("MIRROR_SOURCE_USERNAME is required with MIRROR_SOURCE_TOKEN")
    return Discovery(manifest_repositories(args.manifest), username, token)


@dataclass(frozen=True)
class Source:
    """How one adapter discovers repositories and which filters apply to it."""

    discover: Callable[[argparse.Namespace], Discovery]
    reports_forks: bool = True
    reports_archived: bool = True


SOURCES: dict[str, Source] = {
    "github": Source(_discover_github),
    "bitbucket": Source(_discover_bitbucket, reports_archived=False),
    "codeberg": Source(partial(_discover_forge, "CODEBERG", "https://codeberg.org")),
    "forgejo": Source(partial(_discover_forge, "FORGEJO", None)),
    "gitea": Source(partial(_discover_forge, "SOURCE_GITEA", None)),
    "gitlab": Source(_discover_gitlab),
    "azure": Source(_discover_azure, reports_forks=False, reports_archived=False),
    "manifest": Source(_discover_manifest, reports_forks=False, reports_archived=False),
}


def _build_parser(name: str, source: Source) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Mirror {name} repositories to Gitea")
    parser.set_defaults(exclude_forks=False, exclude_archived=False)
    parser.add_argument("--dry-run", action="store_true",
                        help="list proposed mirrors without creating them")
    parser.add_argument("--include", default="*", metavar="GLOB",
                        help="repository name glob (default: *)")
    if source.reports_forks:
        parser.add_argument("--exclude-forks", action="store_true",
                            help="skip forked repositories")
    if source.reports_archived:
        parser.add_argument("--exclude-archived", action="store_true",
                            help="skip archived repositories")
    if name == "manifest":
        parser.add_argument("--manifest", required=True,
                            help="JSON file with name and clone_url entries")
    parser.add_argument("--mirror-interval", default="24h", metavar="DURATION",
                        help="Gitea mirror interval (default: 24h)")
    return parser


def main(source: str, argv: Sequence[str] | None = None) -> int:
    """Run one source adapter and return the process exit status."""
    if source not in SOURCES:
        raise ValueError(f"Unsupported source adapter: {source}")
    adapter = SOURCES[source]
    args = _build_parser(source, adapter).parse_args(argv)
    try:
        base_url = gitea_base_url(required_env("GITEA_URL"))
        gitea_token = required_env("GITEA_TOKEN")
        owner = required_env("GITEA_ORG_NAME")
        if not _MIRROR_INTERVAL.fullmatch(args.mirror_interval):
            raise MirrorError("--mirror-interval must look like 30m or 12h")
        found = adapter.discover(args)
        selected = select_repositories(found.repositories, pattern=args.include,
                                       exclude_forks=args.exclude_forks,
                                       exclude_archived=args.exclude_archived)
        unique_names(selected)
        created, skipped, failed = mirror_repositories(
            selected, base_url=base_url, token=gitea_token, owner=owner,
            source_username=found.username, source_password=found.password,
            interval=args.mirror_interval, dry_run=args.dry_run)
    except MirrorError as exc:
        _log(f"ERROR: {exc}", error=True)
        return 1
    except KeyboardInterrupt:
        _log("Interrupted", error=True)
        return 130
    _log(f"Summary: discovered={len(found.repositories)} selected={len(selected)} "
         f"created={created} skipped={skipped} failed={failed}")
    return 1 if failed else 0
