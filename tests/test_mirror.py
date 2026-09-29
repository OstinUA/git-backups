"""Offline regression tests for discovery, migration, and deletion safeguards."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import delete
import mirror


class DiscoveryTests(unittest.TestCase):
    def test_github_owner_uses_private_capable_endpoint_and_next_link(self):
        pages = [
            ([{"name": "one", "clone_url": "https://github.com/me/one.git"}],
             {"Link": '<https://api.github.com/user/repos?page=2>; rel="next"'}),
            ([{"name": "two", "clone_url": "https://github.com/me/two.git"}], {}),
        ]
        with patch.object(mirror, "request_json", side_effect=pages) as request:
            repos = mirror.github_repositories("me", "secret", "me")
        self.assertEqual([repo.name for repo in repos], ["one", "two"])
        self.assertIn("/user/repos?", request.call_args_list[0].args[0])
        self.assertEqual(request.call_args_list[1].args[0],
                         "https://api.github.com/user/repos?page=2")

    def test_github_other_user_lists_public_repositories(self):
        with patch.object(mirror, "request_json", return_value=([], {})) as request:
            mirror.github_repositories("me", "secret", "other")
        self.assertIn("/users/other/repos?", request.call_args.args[0])

    def test_bitbucket_selects_https_link_and_slug(self):
        page = {"values": [{"name": "Display Name", "slug": "display-name",
                            "links": {"clone": [
                                {"name": "ssh", "href": "git@bitbucket.org:a/b.git"},
                                {"name": "https", "href": "https://bitbucket.org/a/b.git"}]}}]}
        with patch.object(mirror, "request_json", return_value=(page, {})):
            repos = mirror.bitbucket_repositories("user", "password", "workspace")
        self.assertEqual(repos[0].name, "display-name")
        self.assertEqual(repos[0].clone_url, "https://bitbucket.org/a/b.git")

    def test_rejects_foreign_pagination_host(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.same_origin_page("https://attacker.example/page", "https://api.github.com/user/repos")

    def test_rejects_clone_urls_with_embedded_credentials(self):
        with self.assertRaises(mirror.MirrorError):
            mirror.https_clone_url("https://token@github.com/me/repo.git", "GitHub")

    def test_failed_discovery_cannot_start_migration(self):
        variables = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "secret",
                     "GITEA_ORG_NAME": "backup", "GITHUB_USERNAME": "me",
                     "GITHUB_TOKEN": "source-secret", "GITHUB_TARGET_USERNAME": "me"}
        with patch.dict(os.environ, variables), patch("sys.argv", ["github_to_gitea_mirror.py"]), \
             patch.object(mirror, "github_repositories", side_effect=mirror.MirrorError("page failed")), \
             patch.object(mirror, "mirror_repositories") as migrate, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(mirror.main("github"), 1)
            migrate.assert_not_called()

    def test_filters_forks_archives_and_name(self):
        repos = [mirror.Repository("app-one", "https://x"),
                 mirror.Repository("app-fork", "https://x", fork=True),
                 mirror.Repository("app-old", "https://x", archived=True),
                 mirror.Repository("docs", "https://x")]
        result = mirror.select_repositories(repos, pattern="app-*",
                                            exclude_forks=True, exclude_archived=True)
        self.assertEqual([repo.name for repo in result], ["app-one"])

    def test_forge_adapter_uses_owner_endpoint_and_link_pagination(self):
        pages = [([{"name": "first", "clone_url": "https://codeberg.org/org/first.git"}],
                  {"Link": '<https://codeberg.org/api/v1/orgs/org/repos?page=2>; rel="next"'}),
                 ([{"name": "second", "clone_url": "https://codeberg.org/org/second.git"}], {})]
        with patch.object(mirror, "request_json", side_effect=pages) as request:
            repos = mirror.forge_repositories("https://codeberg.org", "bot", "token", "org", "org")
        self.assertEqual([repo.name for repo in repos], ["first", "second"])
        self.assertIn("/orgs/org/repos", request.call_args_list[0].args[0])

    def test_forge_adapter_uses_current_user_endpoint_for_private_repos(self):
        with patch.object(mirror, "request_json", return_value=([], {})) as request:
            mirror.forge_repositories("https://codeberg.org", "me", "token", "me", "user")
        self.assertIn("/api/v1/user/repos", request.call_args.args[0])

    def test_gitlab_group_pagination_and_namespace_names(self):
        page = [{"path_with_namespace": "team/sub/app", "http_url_to_repo":
                 "https://gitlab.example/team/sub/app.git", "archived": True}]
        with patch.object(mirror, "request_json", return_value=(page, {})) as request:
            repos = mirror.gitlab_repositories("https://gitlab.example", "token", "team/sub")
        self.assertEqual(repos[0].name, "team--sub--app")
        self.assertTrue(repos[0].archived)
        self.assertIn("groups/team%2Fsub/projects", request.call_args.args[0])

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

    def test_manifest_loads_explicit_https_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "repos.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([{"name": "app", "clone_url": "https://example.org/app.git"}], handle)
            repos = mirror.manifest_repositories(path)
        self.assertEqual(repos[0].name, "app")

    def test_manifest_main_rejects_name_collisions_before_network_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "repos.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([{"name": "App", "clone_url": "https://example.org/a.git"},
                           {"name": "app", "clone_url": "https://example.org/b.git"}], handle)
            variables = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "secret",
                         "GITEA_ORG_NAME": "backup"}
            with patch.dict(os.environ, variables), \
                 patch("sys.argv", ["manifest_to_gitea_mirror.py", "--manifest", path]), \
                 patch.object(mirror, "mirror_repositories") as migrate, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(mirror.main("manifest"), 1)
                migrate.assert_not_called()

    def test_gitlab_main_passes_token_for_private_migration(self):
        variables = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "destination-secret",
                     "GITEA_ORG_NAME": "backup", "GITLAB_URL": "gitlab.example",
                     "GITLAB_TOKEN": "source-secret", "GITLAB_GROUP_PATH": "team"}
        with patch.dict(os.environ, variables), patch("sys.argv", ["gitlab_to_gitea_mirror.py"]), \
             patch.object(mirror, "gitlab_repositories", return_value=[]), \
             patch.object(mirror, "mirror_repositories", return_value=(0, 0, 0)) as migrate, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(mirror.main("gitlab"), 0)
        self.assertEqual(migrate.call_args.kwargs["source_username"], "oauth2")
        self.assertEqual(migrate.call_args.kwargs["source_password"], "source-secret")


class MigrationTests(unittest.TestCase):
    def test_existing_repository_is_not_migrated(self):
        with patch.object(mirror, "request_json", return_value=({"name": "one"}, {})) as request, \
             redirect_stdout(io.StringIO()):
            result = mirror.mirror_repositories([mirror.Repository("one", "https://x")],
                                                base_url="https://gitea.example", token="secret",
                                                owner="backup", source_username="me",
                                                source_password="source-secret", interval="24h",
                                                dry_run=False)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(request.call_count, 1)

    def test_dry_run_never_posts_credentials(self):
        with patch.object(mirror, "request_json", return_value=(None, {})) as request:
            with redirect_stdout(io.StringIO()) as output:
                result = mirror.mirror_repositories([mirror.Repository("one", "https://x")],
                                                    base_url="https://gitea.example", token="secret",
                                                    owner="backup", source_username="me",
                                                    source_password="source-secret", interval="24h",
                                                    dry_run=True)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("source-secret", output.getvalue())

    def test_one_failed_migration_does_not_hide_failure(self):
        responses = [(None, {}), mirror.MirrorError("HTTP 500"), (None, {}), ({}, {})]
        with patch.object(mirror, "request_json", side_effect=responses), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = mirror.mirror_repositories(
                [mirror.Repository("one", "https://x"), mirror.Repository("two", "https://y")],
                base_url="https://gitea.example", token="secret", owner="backup",
                source_username="me", source_password="source-secret", interval="24h",
                dry_run=False)
        self.assertEqual(result, (1, 0, 1))


class DeletionTests(unittest.TestCase):
    def test_requires_execute_and_owner_confirmation(self):
        variables = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "secret",
                     "GITEA_ORG_NAME": "backup"}
        with patch.dict(os.environ, variables), patch("sys.argv", ["delete.py", "--repo", "one"]), \
             patch.object(delete._opener, "open") as opener, redirect_stdout(io.StringIO()):
            self.assertEqual(delete.main(), 0)
            opener.assert_not_called()
        with patch.dict(os.environ, variables), \
             patch("sys.argv", ["delete.py", "--repo", "one", "--execute",
                                "--confirm-owner", "wrong"]), \
             patch.object(delete._opener, "open") as opener, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(delete.main(), 1)
            opener.assert_not_called()

    def test_confirmed_deletion_targets_one_repository(self):
        variables = {"GITEA_URL": "gitea.example", "GITEA_TOKEN": "secret",
                     "GITEA_ORG_NAME": "backup"}
        class Response:
            status = 204
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False

        with patch.dict(os.environ, variables), \
             patch("sys.argv", ["delete.py", "--repo", "one", "--execute",
                                "--confirm-owner", "backup"]), \
             patch.object(delete._opener, "open", return_value=Response()) as opener, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(delete.main(), 0)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.args[0].get_method(), "DELETE")
        self.assertEqual(opener.call_args.args[0].full_url,
                         "https://gitea.example/api/v1/repos/backup/one")


if __name__ == "__main__":
    unittest.main()
