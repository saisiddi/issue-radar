# issue-radar

GitHub issue alert and triage tool for a few watched repos. **Alerts and reports only** - it never comments, assigns, labels, or opens PRs on any watched repo, regardless of mode, token source, or config.

- `poll` - alerts on new/changed issues since the last run. Meant to run on a schedule (see GitHub Actions below).
- `sweep` - a one-time Markdown report over every open issue in a repo, for a manual pass over the backlog.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # or requirements.txt if you don't need to run tests
```

Run the tests:

```bash
pytest -q
```

### GitHub authentication

The tool resolves a GitHub token in this order:

1. `gh auth token` - if the [GitHub CLI](https://cli.github.com/) is installed and you're logged in (`gh auth login`), this is used automatically. Nothing to configure.
2. `GITHUB_TOKEN` environment variable (or in a local `.env` file, gitignored - copy `.env.example` to `.env` and fill it in).
3. `GH_PAT` environment variable, same way.

If none are available, requests run unauthenticated (60/hour - not enough for most of these repos' issue counts; `sweep` especially will hit this fast).

The token is never printed or logged; only its source (e.g. "gh CLI") is.

### Notifier credentials (for poll, not needed for `--dry-run`)

Set in `.env` (local) or as GitHub Actions secrets (see below):

- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (default notifier), or
- `DISCORD_WEBHOOK_URL`, with `notifier.type: discord` set in `radar/config.yaml`.

## Running locally

```bash
python -m radar.main poll --dry-run
```

Prints each alert-worthy issue to the terminal instead of sending it, plus a summary table and the number of API requests used. Safe to run repeatedly - it still reads real state from `state.json`/writes it back, so repeated dry-runs behave like repeated real polls for dedupe purposes.

```bash
python -m radar.main sweep --dry-run --repo OWASP/Nettacker
```

Sweeps one repo (omit `--repo`, or repeat the flag, to cover more) and writes `sweep_report_<owner>_<repo>.md`. `--dry-run` also prints the top 15 rows to the terminal.

Both commands take `--config path/to/config.yaml` if you don't want the default at `radar/config.yaml`.

## GitHub Actions (scheduled poll)

The workflow at [.github/workflows/radar.yml](.github/workflows/radar.yml) runs `poll` every 30 minutes and on manual dispatch, then commits the updated `state.json` back to the repo so dedupe state persists between runs.

Two things to know about GitHub's scheduler:
- **Cron can be delayed.** GitHub doesn't guarantee the `*/30 * * * *` schedule fires exactly on time, especially under load - treat "every 30 minutes" as "roughly every 30 minutes."
- **Scheduled workflows on public repos are auto-disabled after 60 days with no repository activity.** The workflow's own `state.json` commits count as activity, so as long as it's actually finding issues to alert on (or at least advancing `last_seen`), it keeps itself alive. If a repo's been quiet long enough that even that stops, re-enable it from the repo's Actions tab.

This repo is **not** pushed anywhere and has no secrets configured yet - none of that is done automatically. When you're ready, here's exactly what to run (adjust the username/repo name and secret values):

```bash
gh repo create your-username/issue-radar --private --source=. --remote=origin
```

```bash
git push -u origin master
```

```bash
gh secret set TELEGRAM_BOT_TOKEN
```

```bash
gh secret set TELEGRAM_CHAT_ID
```

`gh secret set NAME` with no `--body` prompts you to paste the value interactively, so it never ends up in your shell history. Only set the two above if you're using the default Telegram notifier; for Discord instead, set `DISCORD_WEBHOOK_URL` and change `notifier.type` to `discord` in `radar/config.yaml` first.

```bash
gh secret set DISCORD_WEBHOOK_URL
```

`GH_PAT` is optional - only add it if the default `GITHUB_TOKEN` Actions provides (scoped to this one repo) isn't sufficient, which it normally is for reading public repos elsewhere:

```bash
gh secret set GH_PAT
```

After secrets are set, trigger a manual run to confirm everything's wired up before waiting for the cron schedule:

```bash
gh workflow run radar.yml
```

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

Every triaged issue gets exactly one status. Poll alerts on everything except CLAIMED, HAS-PR, and COMMENT-CLAIMED. Sweep additionally keeps COMMENT-CLAIMED (worth a look during a deliberate backlog read, not worth an interrupt).

| Status | Meaning |
|---|---|
| `OPEN-FREE` | No assignee, no linked PR, no other claim signal. |
| `COMMENT-CLAIMED` | Exactly one non-author comment looks like a claim attempt, and nobody's assigned. Not alerted in poll; listed in sweep reports. 2+ such comments is CONTESTED instead. |
| `AUTHOR-CLAIMED` | The reporter appears to intend to do the work themselves - a claim phrase in the issue body or in one of the reporter's *own* follow-up comments (e.g. "I'd like to be assigned", "I have a version ready", "happy to submit a PR"), or a title starting with "Proposal:". Not a hard assignment, just a heads-up not to duplicate effort. |
| `CONTESTED` | 2+ comments from people other than the issue's author look like claim attempts and nobody's assigned. |
| `DISCUSS-ONLY` | A reserved label (`reserved_labels` in config) matched, or the title mentions "GSoC" - treated the same way even without a label, since these repos use that word loosely in titles. Takes priority over AUTHOR-CLAIMED when a title triggers both (e.g. "Proposal: ... - GSoC 2026"). |
| `UNSURE` | A linked PR exists but the evidence is ambiguous (see below) - never silently called "free" on weak evidence. |
| `HAS-PR` | An open or merged same-repo PR clearly references fixing this issue: a closing keyword in its title/body, the issue's own author opened it, or a comment on the issue explicitly names it (e.g. "opened PR #1259", "see #1259", or a pull URL) - the comment case needs no closing keyword, since a human stating it directly is already strong evidence. |
| `CLAIMED` | GitHub assignee is set. |

`UNSURE` covers: a PR mentions the issue without a closing keyword, a referencing PR lives in another repo/fork, and (a known gap) a PR linked only via GitHub's "Link a pull request" sidebar button with no closing keyword *and never mentioned in a comment either* - that action isn't exposed over the REST API we use, so it's invisible here. If this causes noticeable false UNSURE results, GitHub's GraphQL `closedByPullRequestsReferences` field covers it; not implemented yet.

**Comment-mentioned PRs**: GitHub's own cross-reference system only records a backlink on the *mentioned* item's timeline, not on the mentioning issue's own timeline - so a comment like "I opened PR #1259" is otherwise invisible to this tool. To catch it, every comment is scanned for a bare `#N` or a full pull URL, and each candidate number gets fetched (one extra API call per unique mention) to confirm it's actually a PR and get its state. An unresolvable mention (404, deleted, inaccessible) is skipped silently rather than failing the issue.

## Sweep report

`sweep --repo owner/repo` writes `sweep_report_owner_repo.md` - one file per repo, so sweeping several repos (in one call with multiple `--repo` flags, or across separate calls) never overwrites an earlier repo's report.

Within a repo's report, the main table sorts GOOD-fit issues first, then issues a maintainer has already commented on, then by most recent activity. A separate "Old / unanswered" section at the bottom holds issues with no maintainer-reviewer comment and no activity for `sweep.very_old_days_threshold` days (180 by default), so they don't clutter the main table.

Columns include "Maintainer replied?" (any comment from one of that repo's configured `reviewers`) and "Likely already fixed?" (a merged same-repo PR references the issue, even if ambiguous enough to land as UNSURE rather than HAS-PR).

**On issue counts**: the tool's "open issues scanned" count excludes pull requests entirely. A repo's `open_issues_count` on GitHub bundles open issues *and* open PRs together (a well-known REST API quirk - confirmed live against OWASP/Nettacker: GitHub shows 255 there, which is 111 actual open issues + 144 open PRs), so it will read higher than what shows up here. That's expected, not a bug.

## Project rules

- Never comments, assigns, labels, or opens PRs - on any repo, in any mode, regardless of token source.
- `gh` is used strictly to read the auth token (`gh auth token`) - nothing else, anywhere in this codebase.
- Secrets are never printed or logged; only safe-to-log metadata (token source, repo names, issue numbers) appears in output.
- `state.json` and sweep reports are the only files this tool writes locally; the only thing it writes to GitHub is the Actions workflow's own `state.json` commit back to this repo.
