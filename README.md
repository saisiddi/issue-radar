# issue-radar

GitHub issue alert and triage tool. Alerts only - never comments, assigns, labels, or opens PRs on any watched repo.

Full setup docs land with the GitHub Actions workflow (step 6). These sections are here early because they're needed now.

## GitHub authentication

The tool resolves a GitHub token in this order:

1. `gh auth token` - if the [GitHub CLI](https://cli.github.com/) is installed and you're logged in (`gh auth login`), this is used automatically. Nothing to configure.
2. `GITHUB_TOKEN` environment variable (or in a local `.env` file, gitignored - see `.env.example`).
3. `GH_PAT` environment variable, same way.

If none are available, requests run unauthenticated (60/hour - not enough for most of these repos' issue counts).

The token is never printed or logged; only its source (e.g. "gh CLI") is.

This tool only ever makes read-only GitHub API calls (listing issues, comments, and timelines) - it never comments, assigns, labels, or opens PRs, regardless of which token source is used.

## Adding a new repo

Add an entry under `repos:` in [radar/config.yaml](radar/config.yaml):

```yaml
repos:
  - name: owner/repo
    org: OrgName
    reviewers: [maintainer-username]
    requires_assignment: true   # true if a maintainer must assign before a PR is opened
    reserve: false               # true for a backup/lower-priority repo
```

By default a repo uses the global `skills.positive_keywords`, `skills.negative_keywords`, `claim_phrases`, and `reserved_labels` defined at the top level of the config. To override any of these for just one repo (e.g. it uses a different tech stack or has its own claim phrasing), add the field directly on that repo entry:

```yaml
repos:
  - name: owner/rust-project
    org: OrgName
    reviewers: [maintainer-username]
    # Overrides - this repo only matches on these, ignoring the global lists.
    positive_keywords: [rust, cli, parser]
    negative_keywords: [gui]
    claim_phrases: ["i call dibs", "dibs on this"]
    reserved_labels: [maintainer-only]
```

Rules:
- Omit a field (or leave it out entirely) to inherit the global default.
- An explicit list, including an empty `[]`, replaces the global default entirely - it does not merge with it.
- Overrides are matched case-insensitively, same as the global lists.
