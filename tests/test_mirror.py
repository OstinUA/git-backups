"""Offline regression tests for discovery, migration, and deletion safeguards."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import delete
import mirror

GITEA_ENV = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "destination-secret",
             "GITEA_ORG_NAME": "backup"}


class FakeResponse:
    """The subset of ``http.client.HTTPResponse`` that ``request_json`` reads."""

    def __init__(self, status=200, body=b"", headers=()):
        self.status = status
        self.headers = Message()
        for name, value in headers:
            self.headers[name] = value
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self._body[:limit]


class FakeGitea:
    """Answer ``request_json`` calls the way the destination API would."""

    def __init__(self, existing=(), failing=()):
        self.existing = set(existing)
        self.failing = set(failing)
        self.lookups = []
        self.migrations = []

    def __call__(self, url, headers, *, method="GET", payload=None, **_):
        if method == "POST":
            self.migrations.append(payload)
            if payload["repo_name"] in self.failing:
                raise mirror.HTTPStatusError("POST gitea.example", 500)
            return {}, {}
        name = url.rsplit("/", 1)[1]
        self.lookups.append(name)
        if name not in self.existing:
            raise mirror.HTTPStatusError("GET gitea.example", 404)
        return {"name": name}, {}


def http_error(status, body=b""):
    return HTTPError("https://api.example/resource", status, "error", Message(), io.BytesIO(body))


def run_mirror(repositories, gitea, *, dry_run=False, source_password="source-secret"):
    with patch.object(mirror, "request_json", side_effect=gitea), \
         redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
        counts = mirror.mirror_repositories(
            repositories, base_url="https://gitea.example", token="destination-secret",
            owner="backup", source_username="me", source_password=source_password,
            interval="24h", dry_run=dry_run)
    return counts, stdout.getvalue(), stderr.getvalue()


class ConfigurationTests(unittest.TestCase):
    def test_required_env_rejects_missing_and_blank_values(self):
        with patch.dict(os.environ, {"BLANK": "  "}, clear=True):
            for name in ("ABSENT", "BLANK"):
                with self.assertRaises(mirror.MirrorError):
                    mirror.required_env(name)

    def test_env_values_with_control_characters_are_rejected_without_echo(self):
        with patch.dict(os.environ, {"TOKEN": "first\nsecond"}, clear=True):
            with self.assertRaises(mirror.MirrorError) as raised:
                mirror.required_env("TOKEN")
        self.assertNotIn("first", str(raised.exception))

    def test_optional_env_falls_back_for_blank_values(self):
        with patch.dict(os.environ, {"KIND": " "}, clear=True):
            self.assertEqual(mirror.optional_env("KIND", "user"), "user")

    def test_base_url_accepts_hostnames_and_installations_under_a_path(self):
        self.assertEqual(mirror.https_base_url("gitea.example", "URL"), "https://gitea.example")
        self.assertEqual(mirror.https_base_url("https://example.org:8443/gitea/", "URL"),
                         "https://example.org:8443/gitea")

    def test_base_url_rejects_unsafe_values(self):
        for value in ("http://gitea.example", "https://user:pass@gitea.example",
                      "https://token@gitea.example", "https://", "https://[::1",
                      "https://gitea.example:port", "https://gitea.example?x=1",
                      "https://gitea.example/#", "https://gitea .example",
                      "https://gitea.example/\npath"):
            with self.subTest(value=value), self.assertRaises(mirror.MirrorError):
                mirror.https_base_url(value, "URL")

    def test_rejects_clone_urls_with_embedded_credentials(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.https_clone_url("https://token@github.com/me/repo.git", "GitHub")

    def test_rejects_malformed_clone_urls(self):
        for value in (None, 42, "", "git@github.com:me/repo.git", "http://github.com/me/repo.git",
                      "https://github.com/me/repo.git?token=x", "https://[::1/repo.git",
                      "https://github.com/me/re\tpo.git"):
            with self.subTest(value=value), self.assertRaises(mirror.MirrorError):
                mirror.https_clone_url(value, "GitHub")


class TransportTests(unittest.TestCase):
    def test_sends_default_headers_and_json_payload(self):
        response = FakeResponse(201, b'{"id": 1}')
        with patch.object(mirror._opener, "open", return_value=response) as opener:
            data, _ = mirror.request_json("https://gitea.example/api",
                                          {"Authorization": "token t"}, method="POST",
                                          payload={"a": 1}, expected=(201,))
        request = opener.call_args.args[0]
        self.assertEqual(data, {"id": 1})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"a": 1})
        self.assertEqual(request.get_header("User-agent"), mirror.USER_AGENT)
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_caller_headers_override_defaults(self):
        with patch.object(mirror._opener, "open", return_value=FakeResponse()) as opener:
            mirror.request_json("https://api.example/x", {"Accept": "application/vnd.github+json"})
        request = opener.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertIsNone(request.data)
        self.assertFalse(request.has_header("Content-type"))

    def test_status_errors_expose_the_code_but_not_the_body(self):
        error = http_error(403, b'{"message": "token ghp_leaked is invalid"}')
        with patch.object(mirror._opener, "open", side_effect=error):
            with self.assertRaises(mirror.HTTPStatusError) as raised:
                mirror.request_json("https://api.example/x?page=2", {})
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(str(raised.exception), "GET api.example: HTTP 403")

    def test_unexpected_success_status_is_an_error(self):
        with patch.object(mirror._opener, "open", return_value=FakeResponse(200, b"{}")):
            with self.assertRaises(mirror.HTTPStatusError):
                mirror.request_json("https://api.example/x", {}, expected=(201,))

    def test_connection_failures_name_the_cause_only(self):
        failures = {URLError(ConnectionRefusedError("secret detail")): "ConnectionRefusedError",
                    TimeoutError("secret detail"): "TimeoutError",
                    ValueError("Invalid header value b'token secret detail'"): "ValueError"}
        for failure, name in failures.items():
            with self.subTest(name=name), \
                 patch.object(mirror._opener, "open", side_effect=failure):
                with self.assertRaises(mirror.MirrorError) as raised:
                    mirror.request_json("https://api.example/x", {})
                self.assertEqual(str(raised.exception),
                                 f"GET api.example: connection failed ({name})")

    def test_oversized_and_malformed_bodies_are_rejected(self):
        with patch.object(mirror, "MAX_RESPONSE_BYTES", 8), \
             patch.object(mirror._opener, "open", return_value=FakeResponse(200, b"[1, 2, 3, 4]")):
            with self.assertRaises(mirror.MirrorError):
                mirror.request_json("https://api.example/x", {})
        for body in (b"<html>", b"\xff\xfe", b"[" * 100_000):
            with self.subTest(body=body[:8]), \
                 patch.object(mirror._opener, "open", return_value=FakeResponse(200, body)):
                with self.assertRaises(mirror.MirrorError):
                    mirror.request_json("https://api.example/x", {})

    def test_empty_body_and_repeated_headers(self):
        response = FakeResponse(200, b"", [("Link", '<https://a.example/1>; rel="prev"'),
                                           ("LINK", '<https://a.example/3>; rel="next"')])
        with patch.object(mirror._opener, "open", return_value=response):
            data, headers = mirror.request_json("https://a.example/2", {})
        self.assertIsNone(data)
        self.assertEqual(mirror.next_link(headers, "https://a.example/2"), "https://a.example/3")

    def test_only_https_requests_leave_the_process(self):
        with patch.object(mirror._opener, "open") as opener:
            for url in ("http://api.example/x", "file:///etc/passwd", "ftp://api.example/x"):
                with self.subTest(url=url), self.assertRaises(mirror.MirrorError):
                    mirror.request_json(url, {})
        opener.assert_not_called()

    def test_redirects_are_not_followed(self):
        handler = mirror.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {},
                                                   "https://attacker.example/"))


class PaginationTests(unittest.TestCase):
    def test_rejects_foreign_pagination_host(self):
        for target in ("https://attacker.example/page", "http://api.github.com/user/repos",
                       "//attacker.example/page", "https://api.github.com@attacker.example/"):
            with self.subTest(target=target), self.assertRaises(mirror.MirrorError):
                mirror.same_origin_page(target, "https://api.github.com/user/repos")

    def test_resolves_relative_pagination_links(self):
        self.assertEqual(mirror.same_origin_page("/user/repos?page=2",
                                                 "https://api.github.com/user/repos"),
                         "https://api.github.com/user/repos?page=2")

    def test_next_link_understands_rfc_8288_variants(self):
        current = "https://api.example/repos"
        following = "https://api.example/repos?page=2&fields=a,b"
        for header in (f'<{following}>; rel="next"',
                       f"<{following}>; rel=next",
                       f'<{following}>; REL="prev next"',
                       f'<https://api.example/9>; rel="last", <{following}>; rel="next"',
                       f'<{following}>; title="x"; rel="next"'):
            with self.subTest(header=header):
                self.assertEqual(mirror.next_link({"link": header}, current), following)

    def test_next_link_ignores_other_relations(self):
        for header in ("", '<https://api.example/1>; rel="prev"',
                       '<https://api.example/1>; rel="nextish"',
                       '<https://api.example/9>; rel="last", <https://api.example/0>; rel=first'):
            with self.subTest(header=header):
                self.assertEqual(mirror.next_link({"link": header}, "https://api.example/2"), "")

    def test_repeated_page_stops_discovery(self):
        page = ([], {"link": '<https://api.github.com/user/repos?page=1>; rel="next"'})
        with patch.object(mirror, "request_json", side_effect=[page, page, page]) as request:
            with self.assertRaises(mirror.MirrorError):
                mirror.github_repositories("me", "secret", "me")
        self.assertEqual(request.call_count, 2)


class DiscoveryTests(unittest.TestCase):
    def test_github_owner_uses_private_capable_endpoint_and_next_link(self):
        pages = [
            ([{"name": "one", "clone_url": "https://github.com/me/one.git", "fork": True}],
             {"link": '<https://api.github.com/user/repos?page=2>; rel="next"'}),
            ([{"name": "two", "clone_url": "https://github.com/me/two.git", "archived": True}],
             {}),
        ]
        with patch.object(mirror, "request_json", side_effect=pages) as request:
            repos = mirror.github_repositories("me", "secret", "me")
        self.assertEqual(repos, [
            mirror.Repository("one", "https://github.com/me/one.git", fork=True),
            mirror.Repository("two", "https://github.com/me/two.git", archived=True)])
        self.assertIn("/user/repos?", request.call_args_list[0].args[0])
        self.assertEqual(request.call_args_list[1].args[0],
                         "https://api.github.com/user/repos?page=2")
        self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer secret")

    def test_github_other_user_lists_public_repositories(self):
        with patch.object(mirror, "request_json", return_value=([], {})) as request:
            mirror.github_repositories("me", "secret", "other/../x")
        self.assertIn("/users/other%2F..%2Fx/repos?", request.call_args.args[0])

    def test_untyped_api_data_is_reported_not_raised_as_a_crash(self):
        pages = ({"message": "Not Found"}, ["name"], [{"name": 7, "clone_url": "https://x/y.git"}],
                 [{"name": "", "clone_url": "https://x/y.git"}], [{"name": "ok"}],
                 [{"name": "ok", "clone_url": ["https://x/y.git"]}])
        for page in pages:
            with self.subTest(page=page), \
                 patch.object(mirror, "request_json", return_value=(page, {})):
                with self.assertRaises(mirror.MirrorError):
                    mirror.github_repositories("me", "secret", "me")

    def test_bitbucket_selects_https_link_and_slug(self):
        page = {"values": [{"name": "Display Name", "slug": "display-name",
                            "parent": {"slug": "upstream"},
                            "links": {"clone": [
                                {"name": "ssh", "href": "git@bitbucket.org:a/b.git"},
                                {"name": "https", "href": "https://bitbucket.org/a/b.git"}]}}]}
        with patch.object(mirror, "request_json", return_value=(page, {})):
            repos = mirror.bitbucket_repositories("user", "password", "workspace")
        self.assertEqual(repos, [mirror.Repository("display-name", "https://bitbucket.org/a/b.git",
                                                   fork=True)])

    def test_bitbucket_follows_body_pagination_on_the_same_origin_only(self):
        def page(slug, following):
            links = {"clone": [{"name": "https", "href": f"https://bitbucket.org/w/{slug}.git"}]}
            return {"values": [{"slug": slug, "links": links}], "next": following}, {}

        second = "https://api.bitbucket.org/2.0/repositories/w?page=2"
        with patch.object(mirror, "request_json",
                          side_effect=[page("one", second), page("two", None)]) as request:
            repos = mirror.bitbucket_repositories("user", "password", "w")
        self.assertEqual([repo.name for repo in repos], ["one", "two"])
        self.assertEqual(request.call_args.args[0], second)
        with patch.object(mirror, "request_json",
                          return_value=page("one", "https://attacker.example/2")):
            with self.assertRaises(mirror.MirrorError):
                mirror.bitbucket_repositories("user", "password", "w")

    def test_bitbucket_rejects_malformed_repositories(self):
        https_link = {"name": "https", "href": "https://bitbucket.org/a.git"}
        records = ({"slug": "a"}, {"slug": "a", "links": []},
                   {"slug": "a", "links": {"clone": {}}},
                   {"slug": "a", "links": {"clone": ["https"]}},
                   {"slug": 7, "links": {"clone": [https_link]}},
                   {"links": {"clone": [https_link]}})
        for record in records:
            with self.subTest(record=record), \
                 patch.object(mirror, "request_json", return_value=({"values": [record]}, {})):
                with self.assertRaises(mirror.MirrorError):
                    mirror.bitbucket_repositories("user", "password", "workspace")

    def test_account_name_is_dropped_from_clone_urls_but_passwords_are_rejected(self):
        def bitbucket(href):
            page = {"values": [{"slug": "app", "links": {"clone": [{"name": "https",
                                                                    "href": href}]}}]}
            with patch.object(mirror, "request_json", return_value=(page, {})):
                return mirror.bitbucket_repositories("user", "password", "w")[0].clone_url

        def azure(remote_url):
            data = {"value": [{"name": "app", "project": {"name": "Team"},
                               "remoteUrl": remote_url}]}
            with patch.object(mirror, "request_json", return_value=(data, {})):
                return mirror.azure_repositories("org", "pat")[0].clone_url

        self.assertEqual(bitbucket("https://user@bitbucket.org/w/app.git"),
                         "https://bitbucket.org/w/app.git")
        self.assertEqual(azure("https://org@dev.azure.com/org/Team/_git/app"),
                         "https://dev.azure.com/org/Team/_git/app")
        for adapter in (bitbucket, azure):
            with self.subTest(adapter=adapter.__name__), self.assertRaises(mirror.MirrorError):
                adapter("https://user:secret@host.example/w/app.git")

    def test_forge_adapter_uses_owner_endpoint_and_link_pagination(self):
        pages = [([{"name": "first", "clone_url": "https://codeberg.org/org/first.git"}],
                  {"link": '<https://codeberg.org/api/v1/orgs/org/repos?page=2>; rel="next"'}),
                 ([{"name": "second", "clone_url": "https://codeberg.org/org/second.git"}], {})]
        with patch.object(mirror, "request_json", side_effect=pages) as request:
            repos = mirror.forge_repositories("https://codeberg.org", "bot", "token", "org", "org")
        self.assertEqual([repo.name for repo in repos], ["first", "second"])
        self.assertIn("/api/v1/orgs/org/repos?", request.call_args_list[0].args[0])
        self.assertEqual(request.call_args.args[1], {"Authorization": "token token"})

    def test_forge_adapter_uses_current_user_endpoint_for_private_repos(self):
        with patch.object(mirror, "request_json", return_value=([], {})) as request:
            mirror.forge_repositories("https://codeberg.org", "me", "token", "Me", "user")
            mirror.forge_repositories("https://codeberg.org", "me", "token", "other", "user")
        self.assertIn("/api/v1/user/repos?", request.call_args_list[0].args[0])
        self.assertIn("/api/v1/users/other/repos?", request.call_args_list[1].args[0])

    def test_forge_adapter_rejects_unknown_owner_kind(self):
        with patch.object(mirror, "request_json") as request:
            with self.assertRaises(mirror.MirrorError):
                mirror.forge_repositories("https://codeberg.org", "me", "token", "me", "team")
        request.assert_not_called()

    def test_gitlab_group_pagination_and_namespace_names(self):
        page = [{"path_with_namespace": "team/sub/app", "http_url_to_repo":
                 "https://gitlab.example/team/sub/app.git", "archived": True,
                 "forked_from_project": {"id": 1}}]
        with patch.object(mirror, "request_json", return_value=(page, {})) as request:
            repos = mirror.gitlab_repositories("https://gitlab.example", "token", "team/sub")
        self.assertEqual(repos, [mirror.Repository(
            "team--sub--app", "https://gitlab.example/team/sub/app.git",
            fork=True, archived=True)])
        self.assertIn("groups/team%2Fsub/projects", request.call_args.args[0])
        self.assertEqual(request.call_args.args[1], {"PRIVATE-TOKEN": "token"})

    def test_gitlab_without_group_lists_owned_projects(self):
        with patch.object(mirror, "request_json", return_value=([], {})) as request:
            mirror.gitlab_repositories("https://gitlab.example", "token", None)
        self.assertEqual(request.call_args.args[0],
                         "https://gitlab.example/api/v4/projects?owned=true&per_page=100")

    def test_azure_names_include_project_and_duplicate_names_fail(self):
        data = {"value": [{"name": "app", "project": {"name": "Team One"},
                           "remoteUrl": "https://dev.azure.com/org/Team%20One/_git/app"},
                          {"name": "app", "project": {"name": "Team Two"},
                           "remoteUrl": "https://dev.azure.com/org/Team%20Two/_git/app"}]}
        with patch.object(mirror, "request_json", return_value=(data, {})):
            repos = mirror.azure_repositories("org", "pat")
        self.assertEqual([repo.name for repo in repos], ["Team-One--app", "Team-Two--app"])
        mirror.unique_names(repos)
        with self.assertRaises(mirror.MirrorError):
            mirror.unique_names([repos[0], repos[0]])

    def test_azure_rejects_malformed_repositories(self):
        payloads = ([], {"value": None}, {"value": [{"name": "app"}]},
                    {"value": [{"name": "app", "project": {"name": 5}}]},
                    {"value": [{"name": "...", "project": {"name": "..."},
                                "remoteUrl": "https://dev.azure.com/org/p/_git/app"}]})
        for data in payloads:
            with self.subTest(data=data), \
                 patch.object(mirror, "request_json", return_value=(data, {})):
                with self.assertRaises(mirror.MirrorError):
                    mirror.azure_repositories("org", "pat")

    def test_manifest_loads_explicit_https_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "repos.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([{"name": "app", "clone_url": "https://example.org/app.git"}], handle)
            repos = mirror.manifest_repositories(path)
        self.assertEqual(repos, [mirror.Repository("app", "https://example.org/app.git")])

    def test_manifest_rejects_unreadable_or_invalid_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "repos.json")
            with self.assertRaises(mirror.MirrorError):
                mirror.manifest_repositories(path)
            for content in ("{", "{}", '[{"name": ""}]', '[{"name": "a"}]', '["a"]',
                            '[{"name": "a", "clone_url": "https://u:p@example.org/a.git"}]'):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                with self.subTest(content=content), self.assertRaises(mirror.MirrorError):
                    mirror.manifest_repositories(path)

    def test_filters_forks_archives_and_name(self):
        repos = [mirror.Repository("app-one", "https://x"),
                 mirror.Repository("app-fork", "https://x", fork=True),
                 mirror.Repository("app-old", "https://x", archived=True),
                 mirror.Repository("docs", "https://x")]
        result = mirror.select_repositories(repos, pattern="app-*",
                                            exclude_forks=True, exclude_archived=True)
        self.assertEqual([repo.name for repo in result], ["app-one"])
        result = mirror.select_repositories(repos, pattern="*",
                                            exclude_forks=False, exclude_archived=False)
        self.assertEqual(result, repos)


class CommandLineTests(unittest.TestCase):
    def run_main(self, source, argv, variables):
        with patch.dict(os.environ, {**GITEA_ENV, **variables}, clear=True), \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            status = mirror.main(source, argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_failed_discovery_cannot_start_migration(self):
        variables = {"GITHUB_USERNAME": "me", "GITHUB_TOKEN": "source-secret",
                     "GITHUB_TARGET_USERNAME": "me"}
        with patch.object(mirror, "github_repositories",
                          side_effect=mirror.MirrorError("page failed")), \
             patch.object(mirror, "mirror_repositories") as migrate:
            status, _, stderr = self.run_main("github", [], variables)
        self.assertEqual(status, 1)
        self.assertEqual(stderr, "ERROR: page failed\n")
        migrate.assert_not_called()

    def test_destination_settings_are_validated_before_discovery(self):
        variables = {"GITHUB_USERNAME": "me", "GITHUB_TOKEN": "source-secret",
                     "GITHUB_TARGET_USERNAME": "me"}
        with patch.object(mirror, "github_repositories") as discover:
            for override, argv in (({"GITEA_TOKEN": ""}, []),
                                   ({"GITEA_URL": "http://gitea.example"}, []),
                                   ({}, ["--mirror-interval", "daily"])):
                with self.subTest(override=override, argv=argv):
                    status, _, _ = self.run_main("github", argv, {**variables, **override})
                    self.assertEqual(status, 1)
        discover.assert_not_called()

    def test_manifest_main_rejects_name_collisions_before_network_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "repos.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([{"name": "App", "clone_url": "https://example.org/a.git"},
                           {"name": "app", "clone_url": "https://example.org/b.git"}], handle)
            with patch.object(mirror, "mirror_repositories") as migrate:
                status, _, _ = self.run_main("manifest", ["--manifest", path], {})
        self.assertEqual(status, 1)
        migrate.assert_not_called()

    def test_manifest_token_requires_a_username(self):
        with patch.object(mirror, "manifest_repositories") as load:
            status, _, _ = self.run_main("manifest", ["--manifest", "unused.json"],
                                         {"MIRROR_SOURCE_TOKEN": "source-secret"})
        self.assertEqual(status, 1)
        load.assert_not_called()

    def test_gitlab_main_passes_token_for_private_migration(self):
        variables = {"GITLAB_URL": "gitlab.example", "GITLAB_TOKEN": "source-secret",
                     "GITLAB_GROUP_PATH": "team"}
        with patch.object(mirror, "gitlab_repositories", return_value=[]) as discover, \
             patch.object(mirror, "mirror_repositories", return_value=(0, 0, 0)) as migrate:
            status, _, _ = self.run_main("gitlab", [], variables)
        self.assertEqual(status, 0)
        discover.assert_called_once_with("https://gitlab.example", "source-secret", "team")
        self.assertEqual(migrate.call_args.kwargs["source_username"], "oauth2")
        self.assertEqual(migrate.call_args.kwargs["source_password"], "source-secret")

    def test_forge_sources_read_prefixed_settings(self):
        cases = {
            "codeberg": ({"CODEBERG_USERNAME": "me", "CODEBERG_TOKEN": "t"},
                         ("https://codeberg.org", "me", "t", "me", "user")),
            "forgejo": ({"FORGEJO_URL": "forge.example", "FORGEJO_USERNAME": "me",
                         "FORGEJO_TOKEN": "t", "FORGEJO_TARGET_OWNER": "team",
                         "FORGEJO_OWNER_KIND": "ORG"},
                        ("https://forge.example", "me", "t", "team", "org")),
            "gitea": ({"SOURCE_GITEA_URL": "https://src.example/git/",
                       "SOURCE_GITEA_USERNAME": "me", "SOURCE_GITEA_TOKEN": "t"},
                      ("https://src.example/git", "me", "t", "me", "user")),
        }
        for source, (variables, expected) in cases.items():
            with self.subTest(source=source), \
                 patch.object(mirror, "forge_repositories", return_value=[]) as discover, \
                 patch.object(mirror, "mirror_repositories", return_value=(0, 0, 0)):
                status, _, _ = self.run_main(source, ["--dry-run"], variables)
                self.assertEqual(status, 0)
                discover.assert_called_once_with(*expected)

    def test_summary_and_exit_status_reflect_failures(self):
        variables = {"GITHUB_USERNAME": "me", "GITHUB_TOKEN": "source-secret",
                     "GITHUB_TARGET_USERNAME": "me"}
        repos = [mirror.Repository("app", "https://github.com/me/app.git"),
                 mirror.Repository("fork", "https://github.com/me/fork.git", fork=True)]
        with patch.object(mirror, "github_repositories", return_value=repos), \
             patch.object(mirror, "mirror_repositories", return_value=(0, 0, 1)) as migrate:
            status, stdout, _ = self.run_main("github", ["--exclude-forks"], variables)
        self.assertEqual(status, 1)
        self.assertEqual(migrate.call_args.args[0], repos[:1])
        self.assertEqual(stdout, "Summary: discovered=2 selected=1 created=0 skipped=0 failed=1\n")

    def test_filters_are_offered_only_where_the_source_reports_them(self):
        for source, option in (("azure", "--exclude-forks"), ("manifest", "--exclude-forks"),
                               ("bitbucket", "--exclude-archived")):
            with self.subTest(source=source), self.assertRaises(SystemExit):
                self.run_main(source, [option, "--manifest", "unused.json"], {})

    def test_unknown_source_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            mirror.main("subversion", [])


class MigrationTests(unittest.TestCase):
    def test_existing_repository_is_not_migrated(self):
        gitea = FakeGitea(existing={"one"})
        counts, stdout, _ = run_mirror([mirror.Repository("one", "https://x")], gitea)
        self.assertEqual(counts, (0, 1, 0))
        self.assertEqual(stdout, "SKIP backup/one: already exists\n")
        self.assertEqual(gitea.migrations, [])

    def test_dry_run_never_posts_credentials(self):
        gitea = FakeGitea()
        counts, stdout, _ = run_mirror([mirror.Repository("one", "https://x")], gitea,
                                       dry_run=True)
        self.assertEqual(counts, (0, 1, 0))
        self.assertEqual(stdout, "PLAN backup/one\n")
        self.assertEqual(gitea.migrations, [])

    def test_missing_repository_becomes_a_private_mirror(self):
        gitea = FakeGitea()
        counts, stdout, _ = run_mirror([mirror.Repository("one", "https://src.example/one.git")],
                                       gitea)
        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(gitea.migrations, [{
            "clone_addr": "https://src.example/one.git", "repo_name": "one",
            "repo_owner": "backup", "mirror": True, "private": True, "service": "git",
            "mirror_interval": "24h", "auth_username": "me", "auth_password": "source-secret"}])
        self.assertNotIn("source-secret", stdout)

    def test_anonymous_source_sends_no_credentials(self):
        gitea = FakeGitea()
        run_mirror([mirror.Repository("one", "https://x")], gitea, source_password="")
        self.assertNotIn("auth_username", gitea.migrations[0])
        self.assertNotIn("auth_password", gitea.migrations[0])

    def test_one_failed_migration_does_not_hide_failure(self):
        gitea = FakeGitea(failing={"one"})
        repos = [mirror.Repository("one", "https://x"), mirror.Repository("two", "https://y")]
        counts, stdout, stderr = run_mirror(repos, gitea)
        self.assertEqual(counts, (1, 0, 1))
        self.assertEqual(stdout, "CREATE backup/two\n")
        self.assertEqual(stderr, "ERROR backup/one: POST gitea.example: HTTP 500\n")

    def test_failed_existence_check_is_never_treated_as_missing(self):
        def unavailable(url, headers, **_):
            raise mirror.HTTPStatusError("GET gitea.example", 503)

        counts, _, stderr = run_mirror([mirror.Repository("one", "https://x")], unavailable)
        self.assertEqual(counts, (0, 0, 1))
        self.assertIn("HTTP 503", stderr)

    def test_results_follow_repository_order(self):
        names = [f"repo-{index:02}" for index in range(40)]
        gitea = FakeGitea(existing=set(names[::2]))
        counts, stdout, _ = run_mirror([mirror.Repository(name, "https://x") for name in names],
                                       gitea)
        self.assertEqual(counts, (20, 20, 0))
        self.assertEqual([line.split()[1].rstrip(":") for line in stdout.splitlines()],
                         [f"backup/{name}" for name in names])
        self.assertEqual(sorted(gitea.lookups), names)
        self.assertEqual([payload["repo_name"] for payload in gitea.migrations], names[1::2])

    def test_dot_segment_names_never_reach_the_api(self):
        gitea = FakeGitea()
        repos = [mirror.Repository(".", "https://x"), mirror.Repository("..", "https://x")]
        counts, _, _ = run_mirror(repos, gitea)
        self.assertEqual(counts, (0, 0, 2))
        self.assertEqual((gitea.lookups, gitea.migrations), ([], []))

    def test_repository_url_escapes_path_separators(self):
        self.assertEqual(mirror.gitea_repository_url("https://gitea.example", "back up", "a/b?c"),
                         "https://gitea.example/api/v1/repos/back%20up/a%2Fb%3Fc")


class DeletionTests(unittest.TestCase):
    def run_delete(self, argv, response=None):
        with patch.dict(os.environ, GITEA_ENV, clear=True), \
             patch.object(mirror._opener, "open", return_value=response) as opener, \
             redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            status = delete.main(argv)
        return status, stdout.getvalue(), opener

    def test_requires_execute_and_owner_confirmation(self):
        status, stdout, opener = self.run_delete(["--repo", "one"])
        self.assertEqual(status, 0)
        self.assertIn("PLAN DELETE backup/one", stdout)
        opener.assert_not_called()
        for confirmation in ([], ["--confirm-owner", "wrong"], ["--confirm-owner", "Backup"]):
            with self.subTest(confirmation=confirmation):
                status, _, opener = self.run_delete(["--repo", "one", "--execute", *confirmation])
                self.assertEqual(status, 1)
                opener.assert_not_called()

    def test_confirmed_deletion_targets_one_repository(self):
        status, stdout, opener = self.run_delete(
            ["--repo", "one", "--execute", "--confirm-owner", "backup"], FakeResponse(204))
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "DELETED backup/one\n")
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.full_url, "https://gitea.example/api/v1/repos/backup/one")
        self.assertEqual(request.get_header("Authorization"), "token destination-secret")

    def test_unexpected_status_is_not_reported_as_deleted(self):
        status, stdout, _ = self.run_delete(
            ["--repo", "one", "--execute", "--confirm-owner", "backup"], FakeResponse(200))
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")

    def test_dot_segment_repository_is_rejected(self):
        status, _, opener = self.run_delete(
            ["--repo", "..", "--execute", "--confirm-owner", "backup"], FakeResponse(204))
        self.assertEqual(status, 1)
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
