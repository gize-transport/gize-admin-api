# Prompts for Claude Code

## Before you start (you, not Claude)

1. Create an empty private repo `gize-admin-api` in the company GitHub Organization and clone it.
2. Unzip this kit into the repo root (`CLAUDE.md`, `PROMPT.md`, `docs/`, `.github/` at the top level). Commit it to `main`.
3. Enable the hook that blocks direct pushes to `main`: `git config core.hooksPath .githooks` (once per clone). In **Settings → General → Pull Requests**, tick **Allow squash merging** and **Automatically delete head branches**.
4. Install Docker, `uv`, and the GitHub CLI (`gh auth login`) so Claude Code can open PRs.
5. Start Claude Code in the repo root: `claude`.

## First message (paste everything below the line)

---

You are the backend engineer for the Gize Admin API. Read `CLAUDE.md`, then every file in `docs/` in this order: `product-brief.md`, `architecture.md`, `engineering-standards.md`, `workflow.md`, `milestones.md`, and `.github/pull_request_template.md`.

Do not write code yet. Reply with:
1. A short summary of the product, architecture and standards in your own words, so I can check we are aligned.
2. Anything in the docs that is unclear, contradictory or risky, with your recommendation for each.
3. The plan for **GZ-001** using the plan template in `docs/workflow.md`.

From then on, follow `docs/workflow.md` for every ticket, in milestone order:
- Post the plan and **wait for my approval** before writing code.
- Tests first, then implementation, then the full self-review checklist.
- One ticket = one branch = one PR titled `GZ-xxx: <title>`, opened with `gh pr create` using our PR template. Update the ticket's status in `docs/milestones.md` in the same PR.
- Stop after opening each PR and tell me: what changed, how it was tested, any checklist items you could not tick, and the next ticket you propose.

Never weaken a standard, a test or a CI check to make progress. If something blocks you, stop and ask.

---

## For every next ticket

> Next ticket: GZ-0xx. Post the plan and wait for approval.

## When reviewing a PR you don't want to read line by line

> Review PR #<n> for GZ-0xx as a strict senior reviewer against docs/engineering-standards.md. List every violation with file and line, then the tests that are missing from the required set. Don't fix anything yet.

Run this in a **fresh** Claude Code session so the reviewer hasn't seen the code being written.

## Build the whole MVP (milestone mode)

Use this instead of going ticket by ticket. You approve once per milestone; Claude Code then works through that milestone's tickets on its own, still one PR per ticket, and stops at the end of each milestone.

Before using it: make sure the pre-push hook is enabled (`git config core.hooksPath .githooks`) and that CI is running on pull requests. Claude Code merges each PR itself, only after every CI check passes.

Paste everything below the line:

---

Build the Gize Admin API MVP: milestones **M0 to M5** in `docs/milestones.md`, plus **GZ-060** (authorization matrix test), **GZ-061** (OpenAPI completeness) and **GZ-064** (overview summary endpoint). Keep `openapi.json` and the `api-v*` tags up to date as described in `docs/api-contract.md` – the admin frontend repo depends on them. Follow `CLAUDE.md` and every document in `docs/`. The engineering standards are non-negotiable.

Work in milestone mode:

1. **Milestone plan – then stop.** At the start of each milestone, post one plan covering all its tickets, using the plan template in `docs/workflow.md` for each ticket, plus the order you'll do them in and any open questions. Wait for my approval. Do not start coding a milestone I haven't approved.
2. **Execute tickets in order.** For each ticket in the approved milestone:
   - Branch from up-to-date `main`: `feat/GZ-xxx-short-name`.
   - Tests first (happy path, unauthenticated, wrong role, wrong owner, invalid input, dependency failure), show they fail, then implement.
   - Run ruff, mypy `--strict` and the full test suite with coverage; all must pass locally.
   - Complete the self-review checklist honestly.
   - **Independent review:** start a subagent with no context of your implementation and ask it to review the diff strictly against `docs/engineering-standards.md` and the ticket's acceptance criteria. Fix everything it finds, or explain in the PR why a finding doesn't apply.
   - Open the PR with `gh pr create` (title `GZ-xxx: <title>`, our PR template, checklist filled in, review findings summarised), update the ticket status in `docs/milestones.md` in the same PR, then wait for CI with `gh pr checks <number> --watch`.
   - Merge only when every check is green: `gh pr merge <number> --squash --delete-branch`. If any check fails, fix the cause on the same branch and wait again. Never merge with failing or pending checks.
3. **End of milestone – stop.** Report: tickets merged with PR links, endpoints added, migrations added, anything you could not satisfy or deferred (as new tickets), risks you see, and the plan for the next milestone.

**Stop immediately and ask me** if: a standard would have to be broken; a business rule is missing or unclear (e.g. waitlist threshold, SMS provider); you need a new dependency, external service or environment variable; a migration would be destructive or not backward-compatible; CI fails twice for a reason you can't fix within the ticket; or a ticket turns out much bigger than its description.

Never weaken, skip or delete a test, lint rule, type check or CI step to make progress. Never commit to `main` directly. Never put real secrets in the repo; use `.env.example` placeholders and tell me what to set.

Start now with the milestone plan for **M0 – Foundation**.

---
