# Workflow: one ticket, one branch, one PR

Every piece of work is a ticket in `docs/milestones.md` (IDs like `GZ-012`). Work on one ticket at a time, in milestone order, unless told otherwise.

## The loop

1. **Pick** the next ticket whose dependencies are done. Set its status to `in progress` in `milestones.md`.
2. **Plan – then stop.** Post a plan using the template below and wait for explicit approval. Do not write code before approval.
3. **Branch:** `feat/GZ-012-short-name` (or `fix/…`, `chore/…`) from up-to-date `main`.
4. **Tests first.** Write the failing tests from the plan's test list (happy path, unauthenticated, wrong role, wrong owner, invalid input, dependency failure). Run them and show they fail for the right reason.
5. **Implement** the smallest change that makes them pass, following `architecture.md` layers.
6. **Migrations** (if any): generate with Alembic, review the SQL, write the downgrade, run upgrade → downgrade → upgrade locally.
7. **Run everything:** `ruff`, `mypy --strict`, full `pytest` with coverage. All green.
8. **Self-review** with the checklist below. Fix anything that fails; mention anything you could not satisfy.
9. **Open the PR** with the template, titled `GZ-012: <ticket title>`. Set the ticket status to `in review`.
10. After merge, set status to `done` and note any follow-up tickets you discovered (add them to `milestones.md` with new IDs; never silently expand scope).

## Plan template (post this before coding)

```
## GZ-012 – <title>
Goal: <one sentence>
Out of scope: <what this ticket will not do>

Endpoints: METHOD /v1/... – role(s) – idempotent? – rate limit
Data: tables/columns added or changed; indexes; migration type (expand / contract)
Business rules: numbered list, each mapped to where it is enforced (service function)
Authorization: per endpoint – role check + object/ownership check
Transactions & concurrency: what runs in one transaction; locks; isolation if not default
Failure modes: each dependency and what the API does when it fails or times out
Observability: new log events, metrics, security events
Test list: every test case by name
Open questions: anything to confirm before coding
```

## Self-review checklist (copy into the PR and tick honestly)

Security
- [ ] Every new handler checks role **and** object ownership/state; public endpoints listed
- [ ] All input schemas use `extra="forbid"` with length/size/range limits
- [ ] No raw SQL / string-built queries / shell calls (or each one justified)
- [ ] No secrets, tokens, names, children's data or full phones in logs, errors or responses
- [ ] Error responses are problem+json with code + request ID only
- [ ] Outbound calls only via `core/http.py` to allowlisted hosts

Database
- [ ] Multi-step writes in one transaction; locks where concurrency matters
- [ ] Lists paginated and bounded; no N+1 (query-count test added); indexes added and named
- [ ] Migration is expand/contract, has a tested downgrade, safe with the currently deployed code
- [ ] Audit log row written for every staff write; soft delete respected

Reliability
- [ ] Timeouts on every new network call
- [ ] Retries only on idempotent operations, with backoff + jitter
- [ ] Idempotency-Key required where clients may retry side-effecting POSTs

Rate limiting
- [ ] Limits set for new endpoints; 429 + Retry-After; fail-open/closed behaviour correct

Observability
- [ ] Request ID and staff ID on logs; new metrics/traces present; security events logged

API contract
- [ ] `openapi.json` regenerated and committed; `oasdiff` shows no breaking change (or approved)
- [ ] New error codes added to the schema enum; operationIds stable

Testing
- [ ] Happy path, unauthenticated, wrong role, wrong owner, invalid input, dependency failure
- [ ] Real Postgres; no mocked DB; concurrency test where relevant
- [ ] Coverage gate passes; ruff + mypy strict clean

## Definition of done

The ticket's acceptance criteria are met, the checklist is fully ticked (or exceptions are explained and approved), CI is green, the PR is approved and merged, and `milestones.md` is updated.

## When to stop and ask

- A ticket seems to need breaking an engineering standard.
- A business rule is missing, ambiguous or contradicts `product-brief.md`.
- A new dependency, new external service or new environment variable is needed.
- A migration would be destructive or not backward-compatible.
- You discover work outside the ticket's scope (propose a new ticket instead).
