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

## Statuses

Every triaged issue gets exactly one status. Poll and sweep both alert/keep everything except CLAIMED and HAS-PR - someone's already on those.

| Status | Meaning |
|---|---|
| `OPEN-FREE` | No assignee, no linked PR, no other claim signal. |
| `AUTHOR-CLAIMED` | The reporter appears to intend to do the work themselves - either a claim phrase in the issue body (e.g. "I'd like to work on this", "happy to submit a PR") or a title starting with "Proposal:". Not a hard assignment, just a heads-up not to duplicate effort. |
| `CONTESTED` | 2+ comments look like claim attempts and nobody's assigned. |
| `DISCUSS-ONLY` | A reserved label (`reserved_labels` in config) matched, or the title mentions "GSoC" - treated the same way even without a label, since these repos use that word loosely in titles. |
| `UNSURE` | A linked PR exists but the evidence is ambiguous (see below) - never silently called "free" on weak evidence. |
| `HAS-PR` | An open or merged same-repo PR clearly references fixing this issue (closing keyword in title/body, or the issue's own author opened the PR). |
| `CLAIMED` | GitHub assignee is set. |

`UNSURE` covers: a PR mentions the issue without a closing keyword, a referencing PR lives in another repo/fork, and (a known gap) a PR linked only via GitHub's "Link a pull request" sidebar button with no closing keyword - that action isn't exposed over the REST API we use, so it's invisible here. If this causes noticeable false UNSURE results, GitHub's GraphQL `closedByPullRequestsReferences` field covers it; not implemented yet.

## Sweep report

`sweep_report.md` groups by repo. Within a repo, the main table sorts GOOD-fit issues first, then issues a maintainer has already commented on, then by most recent activity - each repo also gets a separate "Old / unanswered" section at the bottom for issues with no maintainer-reviewer comment and no activity for `sweep.very_old_days_threshold` days (180 by default), so they don't clutter the main table.

Columns include "Maintainer replied?" (any comment from one of that repo's configured `reviewers`) and "Likely already fixed?" (a merged same-repo PR references the issue, even if ambiguous enough to land as UNSURE rather than HAS-PR).

**On issue counts**: the tool's "open issues scanned" count excludes pull requests entirely. A repo's `open_issues_count` on GitHub bundles open issues *and* open PRs together (a well-known REST API quirk), so it will read higher than what shows up here - that's expected.
