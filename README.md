# Git repository mirrors for Gitea

Create private Gitea pull mirrors from GitHub, Bitbucket Cloud, GitLab, Codeberg, Forgejo, another Gitea instance, Azure DevOps, or an explicit list of HTTPS Git URLs. Python 3.11 or newer is required; the scripts use only the standard library.

The scripts create missing mirrors. Gitea performs subsequent synchronization at the configured interval. Existing destination repositories are skipped and never modified. Repository discovery completes before creation starts, so a failed source API page cannot silently produce a partial backup.

## Supported sources

| Source | Script | Discovery scope |
| --- | --- | --- |
| GitHub | `github_to_gitea_mirror.py` | Repositories owned by an account |
| Bitbucket Cloud | `bitbucket_to_gitea_mirror.py` | Repositories in one workspace |
| GitLab.com or self-managed GitLab | `gitlab_to_gitea_mirror.py` | Projects owned by the token user, or a group and its subgroups |
| Codeberg | `codeberg_to_gitea_mirror.py` | Repositories owned by a user or organization |
| Forgejo | `forgejo_to_gitea_mirror.py` | Repositories owned by a user or organization |
| Another Gitea instance | `gitea_to_gitea_mirror.py` | Repositories owned by a user or organization |
| Azure DevOps Services | `azure_devops_to_gitea_mirror.py` | Git repositories across an accessible organization |
| Any HTTPS Git host | `manifest_to_gitea_mirror.py` | Explicit repositories in a JSON manifest; no automatic discovery |

This is an extensible set of adapters, not a claim that every Git hosting service has a compatible discovery API. The manifest adapter covers hosts without a dedicated adapter.

## Security and prerequisites

- Provide credentials through environment variables or Gitea Actions secrets. Never place tokens in source code, clone URLs, manifest files, or command-line arguments.
- Use HTTPS for the source and destination. The scripts reject clone URLs containing embedded credentials and refuse API pagination links to another host.
- Give source credentials read access to the repositories being mirrored. Give the destination Gitea token permission to create repositories in the target organization.
- A Gitea token was previously committed directly in `delete.py`. **Revoke that old token and issue a replacement.** Removing it from the current file does not invalidate copies or repository history.
- These are Git pull mirrors. The generic `git` migration service is not intended to back up host-specific issues, pull requests, CI settings, or other metadata.

## Common configuration

All mirror scripts require:

| Environment variable | Purpose |
| --- | --- |
| `GITEA_URL` | Destination HTTPS URL, such as `https://gitea.example.org` |
| `GITEA_TOKEN` | Destination Gitea API token |
| `GITEA_ORG_NAME` | Destination organization |

Each script also requires source-specific settings:

| Source | Required environment variables | Optional environment variables |
| --- | --- | --- |
| GitHub | `GITHUB_USERNAME`, `GITHUB_TOKEN`, `GITHUB_TARGET_USERNAME` | None |
| Bitbucket Cloud | `BITBUCKET_USERNAME`, `BITBUCKET_APP_PASSWORD`, `BITBUCKET_TARGET_USERNAME` | None |
| GitLab | `GITLAB_URL`, `GITLAB_TOKEN` | `GITLAB_GROUP_PATH` |
| Codeberg | `CODEBERG_USERNAME`, `CODEBERG_TOKEN` | `CODEBERG_TARGET_OWNER`, `CODEBERG_OWNER_KIND` |
| Forgejo | `FORGEJO_URL`, `FORGEJO_USERNAME`, `FORGEJO_TOKEN` | `FORGEJO_TARGET_OWNER`, `FORGEJO_OWNER_KIND` |
| Gitea source | `SOURCE_GITEA_URL`, `SOURCE_GITEA_USERNAME`, `SOURCE_GITEA_TOKEN` | `SOURCE_GITEA_TARGET_OWNER`, `SOURCE_GITEA_OWNER_KIND` |
| Azure DevOps | `AZURE_DEVOPS_ORGANIZATION`, `AZURE_DEVOPS_PAT` | None |
| Manifest | `--manifest PATH` | `MIRROR_SOURCE_USERNAME`, `MIRROR_SOURCE_TOKEN` for private repositories |

`*_OWNER_KIND` is `user` by default and may be set to `org`. `*_TARGET_OWNER` defaults to the source username. For an organization, set both the owner name and kind. `GITLAB_GROUP_PATH` may contain nested groups, such as `team/platform`. When it is absent, GitLab lists projects explicitly owned by the token user. Use a token that can access private repositories when they must be included.

For GitHub, a target matching `GITHUB_USERNAME` uses `/user/repos`, which can enumerate accessible private repositories. A different target uses `/users/{name}/repos`, which [lists public repositories only](https://docs.github.com/en/rest/repos/repos). `BITBUCKET_TARGET_USERNAME` is the Bitbucket workspace ID. Use credentials compatible with Bitbucket HTTP Basic authentication.

## Run manually

Set the common and source-specific environment variables, then preview and run a source:

```bash
python gitlab_to_gitea_mirror.py --dry-run
python gitlab_to_gitea_mirror.py --exclude-archived
python codeberg_to_gitea_mirror.py --include 'team-*'
python azure_devops_to_gitea_mirror.py --mirror-interval 12h
```

Common options:

| Option | Effect |
| --- | --- |
| `--dry-run` | Check the destination and print planned mirrors without creating them |
| `--include GLOB` | Select repository names matching a shell-style pattern |
| `--exclude-forks` | Skip forks where the source API exposes that property |
| `--exclude-archived` | Skip archived repositories on supported sources |
| `--mirror-interval 12h` | Set the Gitea synchronization interval; default `24h` |

The GitLab adapter names destination repositories with their complete namespace path joined by `--`, for example `team--platform--app`. Azure DevOps names them `Project--Repository`. This prevents common name collisions across groups or projects. Any remaining case-insensitive name collision stops the run before a mirror is created.

Each run reports discovered, selected, created, skipped, and failed counts. Any discovery error stops the run. Individual migration errors are reported while other selected repositories continue; the final exit code is `1` when any migration fails.

### Manifest for other Git hosts

Copy [repositories.example.json](repositories.example.json) and replace its entries with your repositories:

```json
[
  {"name": "project-one", "clone_url": "https://git.example.org/team/project-one.git"}
]
```

Then run:

```bash
python manifest_to_gitea_mirror.py --manifest repositories.json --dry-run
python manifest_to_gitea_mirror.py --manifest repositories.json
```

For private repositories, set `MIRROR_SOURCE_USERNAME` and `MIRROR_SOURCE_TOKEN` to credentials that work for every URL in the manifest. Keep the manifest itself free of secrets. A manifest explicitly lists repositories; adding a repository at the source requires adding another entry.

## Gitea Actions

The workflows under `.gitea/workflows/` run daily and support manual dispatch. `github.yaml` and `bitbucket.yaml` retain their existing secret and variable names; disable either workflow in Gitea when that source is unused. `additional-sources.yaml` contains jobs for Codeberg, GitLab, Forgejo, another Gitea instance, and Azure DevOps. Each additional job is disabled until its `ENABLE_CODEBERG`, `ENABLE_GITLAB`, `ENABLE_FORGEJO`, `ENABLE_SOURCE_GITEA`, or `ENABLE_AZURE_DEVOPS` repository variable is set to `true`.

Configure the common destination settings as the `URL_GITEA` variable and `TOKEN_GITEA` secret. For each enabled source, configure the source variables and secrets shown in the job's `env` block, plus its `ORG_NAME_FOR_*` destination organization variable. The GitHub and Bitbucket workflows can send failure email using `SMTP_SERVER`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `EMAIL_TO`; additional-source failures appear in the Actions run status.

The runner must be able to reach each enabled source and the destination Gitea instance over HTTPS.

## Delete one destination repository

`delete.py` needs `GITEA_URL`, `GITEA_TOKEN`, and `GITEA_ORG_NAME`. It never enumerates or bulk-deletes repositories:

```bash
python delete.py --repo project-one
python delete.py --repo project-one --execute --confirm-owner MyGiteaOrg
```

Without `--execute`, it only prints the deletion plan. The confirmation must exactly match `GITEA_ORG_NAME`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests replace remote API responses and never use real account credentials. A live `--dry-run` with your configuration is the next check before relying on scheduled backups.
