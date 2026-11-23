## Ticket
GZ-___ – <title> (from docs/milestones.md)

## What changed
-

## Endpoints
| Method | Path | Roles | Idempotent | Rate limit |
| --- | --- | --- | --- | --- |

## Database
Migration: none / expand / contract · Downgrade tested: yes / no · New indexes:

## API contract
Changed: yes / no · Additive / breaking (approved) · Frontend ticket needed: GZW-___
Contract version produced: api-v_._._ / unchanged

## How it was tested
-

## Independent review
Reviewer: subagent with no implementation context / human · Scope: diff vs `docs/engineering-standards.md` + acceptance criteria
| Finding | Resolution (fixed in <commit> / does not apply because …) |
| --- | --- |

## Self-review checklist
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
- [ ] Migration is expand/contract, has a tested downgrade, safe with the currently deployed code (production's too: it lags staging; a contract step waits until production runs the release that stopped using what it removes)
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

## Risks and follow-ups
-
