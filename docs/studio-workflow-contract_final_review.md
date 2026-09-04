# Studio workflow API final review

- Reviewed at: 2026-09-04 17:45 KST
- Contract: `docs/studio-workflow-contract.md`
- Stack: FastAPI, SQLAlchemy, PostgreSQL/SQLite-compatible tests
- Test strategy: in-process API and persistence tests; paid provider submission disabled

## Verdict

The local P0/P1 Studio contract is accepted after Critical/Major remediation.
It is not approved for unauthenticated public deployment: durable execution,
provider poll recovery, object storage, authorization, and Korean forced
alignment remain explicit production blockers.

## Acceptance checklist

| Area | Result | Evidence |
| --- | --- | --- |
| Versioned 4/6/8/15 templates | PASS | Exact plans and integral provider duration tests |
| Pre-generation quote | PASS | 15-minute persisted quote, capability preflight, fixed snapshot |
| Spend approval | PASS | `total.max` is the per-click approval ceiling; zero automatic paid retries |
| Studio submit safety | PASS | `quote_id` and `client_request_id` required with `template_id` |
| Idempotency | PASS | Same canonical request replays; changed body returns 409 |
| Resolution consistency | PASS | Quoted `1080p` snapshot reaches the provider request |
| Retry safety | PASS | Active parent and non-retryable failures return 409 |
| Failure privacy | PASS | Public list/detail use stable messages; raw evidence remains persisted |
| FE option recovery | PASS | Five allowlisted Studio controls are stored and returned under `options` |
| Legacy artifacts | PASS | Top-level completed output normalizes to one playable `legacy-primary` item |
| List query support | PASS | Created-time and status/created-time composite indexes created at startup |
| Full regression | PASS | 265 tests passed in 14.862 seconds |

## Critical and Major findings resolved

1. Template generation previously allowed an unquoted, non-idempotent paid
   submission. Both identifiers are now conditionally mandatory.
2. A quote could previously be issued for a duration or resolution unsupported
   by the selected model. A read-only catalog preflight now fails closed with a
   stable 502 or 422 before quote persistence.
3. Runtime settings could replace the quoted resolution. The stored request
   snapshot now supplies the provider resolution.
4. Candidate retry could race the active worker or resubmit provider work after
   timeout/shared-stage failure. Terminal-parent and explicit retryability gates
   now reject those paths; unresolved siblings also prevent terminalization.
5. Raw provider errors and local paths could appear in detail/list responses.
   Response projection now emits allowlisted public codes and messages only.
6. Integral JSON float durations such as `15.0` could fail at the provider
   boundary. They normalize to an integer while fractional durations still fail.
7. The previous default of three candidates expanded legacy spend. It is now
   one unless the caller explicitly requests more.

## Data and performance review

- Quotes are immutable snapshots and generation jobs retain their full private
  request and failure evidence for worker use.
- Public projections are assembled from allowlists; neither payload JSON nor
  raw error text is returned.
- The management query has composite indexes for `(created_at, job_id)` and
  `(status, created_at, job_id)`.
- Legacy output normalization is response-only and does not rewrite existing
  database rows.
- Startup DDL remains the existing compatibility mechanism. Alembic or another
  transactional migration system is still required before multi-instance
  production rollout.

## Test execution

Focused contract and regression set:

```bash
.venv/bin/python -m unittest \
  tests.test_generation_templates \
  tests.test_generation_quotes \
  tests.test_generation_listing \
  tests.test_generation_jobs \
  tests.test_studio_workflow \
  tests.test_runtime_config \
  tests.test_final_generation \
  tests.test_video_generator \
  tests.test_settings
```

Result: 130 tests passed.

Full suite:

```bash
.venv/bin/python -m unittest discover -s tests
```

Result: 265 tests passed. The prior SQLite cross-thread connection cleanup
traceback is gone. Existing non-failing Starlette deprecation and mocked
`HTTPError` resource warnings remain.

## User review points

- Confirm that `total.max`, not `total.expected`, is the amount shown at the
  final generation confirmation.
- Confirm that retry-disabled errors lead to provider reconciliation or a fresh
  quote instead of a one-click resubmit.
- Confirm whether legacy artifacts should remain labeled `legacy-primary` in
  the frontend or receive a localized display label.

## Remaining risks and assumptions

- FastAPI `BackgroundTasks` is not a durable worker and cannot resume after a
  process restart.
- Provider polling URLs are not persisted, so submitted jobs cannot yet be
  reconciled automatically.
- Narration and final media remain host-local rather than checksummed object
  storage artifacts.
- No authentication or ownership boundary protects the management APIs yet.
- Korean audio has duration fitting but no word-level forced-alignment proof.
- This review made no paid script, TTS, or video generation request.
