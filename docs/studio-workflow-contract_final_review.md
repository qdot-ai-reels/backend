# Studio workflow API final review

- Reviewed at: 2026-09-04 23:15 KST
- Contract: `docs/studio-workflow-contract.md`
- Stack: FastAPI, SQLAlchemy, PostgreSQL/SQLite-compatible tests
- Test strategy: in-process API and persistence tests; paid provider submission disabled

## Verdict

The local single-process Studio contract, including the advertising-product
catalog and its frontend workflow, is accepted after Critical/Major
remediation. It is not approved for public production deployment: operator
authorization, immutable owned asset ingestion, durable execution, provider
poll recovery, object storage, and Korean forced alignment remain explicit
production blockers.

## Acceptance checklist

| Area | Result | Evidence |
| --- | --- | --- |
| Versioned 4/6/8/15 templates | PASS | Exact plans and integral provider duration tests |
| Pre-generation quote | PASS | 15-minute persisted quote, capability preflight, fixed snapshot |
| Spend forecast | PARTIAL | `total.max` is the displayed video-provider upper estimate and automatic paid retries are zero; a provider/account hard cap and full-pipeline ledger remain deferred |
| Studio submit safety | PASS | `quote_id` and `client_request_id` required with `template_id` |
| Idempotency | PASS | Same canonical request replays; changed body returns 409 |
| Resolution consistency | PASS | Quoted `1080p` snapshot reaches the provider request |
| Retry safety | PASS | Active parent and non-retryable failures return 409 |
| Failure privacy | PASS | Public list/detail use stable messages; raw evidence remains persisted |
| FE option recovery | PASS | Five allowlisted Studio controls are stored and returned under `options` |
| Advertising product catalog | PASS | Inactive-first registration, explicit review activation, revision conflicts, recoverable archive, and audit history |
| Product-to-generation integrity | PASS | Quote and submit pin the active product revision; submit and worker recheck it before paid work |
| Paid candidate retry | PASS | Current quote authorizes zero retries and returns `PAID_RETRY_QUOTE_REQUIRED` before provider work |
| Local loopback CORS | PASS | Configured `localhost`/`127.0.0.1` aliases interoperate without broadening non-local origins |
| Browser workflow | PASS | `/products` add/JSON-prefill/cancel and `/create` active-product selection; no console errors or mutation |
| Legacy artifacts | PASS | Top-level completed output normalizes to one playable `legacy-primary` item |
| List query support | PASS | Created-time and status/created-time composite indexes created at startup |
| Full regression | PASS | Backend 316 tests; frontend 33 contract tests plus lint, TypeScript, and production build |

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
8. Studio previously depended on source-controlled product fixtures. Products
   now have an operator workflow with inactive-first saves, explicit semantic
   review, optimistic revisions, activation audit, and recoverable archive.
9. A product could change after quote or initial request validation. The
   catalog revision is now pinned and rechecked inside initial job reservation,
   retry reservation, and the worker immediately before paid provider work.
10. A paid candidate retry was not represented by the current quote. Current
    quotes authorize zero paid retries, and Studio rejects them with a stable
    409 until an explicit retry quote contract exists.
11. The documented frontend URL used `127.0.0.1` while the backend's common
    local CORS value used `localhost`. Loopback aliases are now normalized
    without modifying or broadening configured production origins.
12. Rejected optional detail-image logs previously retained URL userinfo. They
    now retain only the host label, with credentials, path, query, and fragment
    excluded and covered by regression.

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

Result: 316 tests passed in 16.271 seconds. The prior SQLite cross-thread
connection cleanup traceback is gone. Existing non-failing Starlette
deprecation and mocked `HTTPError` resource warnings remain.

Frontend gates:

```bash
npm run lint
npx tsc --noEmit
npm run test:contracts
npm run build
```

Result: 33 contract tests passed; lint, TypeScript, and the Next.js production
build passed. The route manifest includes `/products`.

Local browser smoke at `http://127.0.0.1:3000`:

- `/products` displayed one seeded active product and the management menu.
- The add dialog exposed core fields, image controls, review notes, and JSON
  prefill. Known fields populated successfully; cancel left the catalog at one
  total/one active product.
- `/create` selected the active product and exposed the product-management link.
- No save, activation, archive, generation, or paid provider request occurred.
- No visible or browser-console error remained after the loopback CORS fix.

## User review points

- Confirm that both `total.expected` and `total.max` are shown as video-provider
  estimates at final confirmation, without describing either as a guaranteed
  provider/account hard cap.
- Confirm that retry-disabled errors lead to provider reconciliation or a fresh
  quote instead of a one-click resubmit.
- Confirm whether legacy artifacts should remain labeled `legacy-primary` in
  the frontend or receive a localized display label.
- Confirm the product-operator role, semantic-review ownership, and approval
  evidence required before a catalog revision may become active.

## Remaining risks and assumptions

- Product mutation APIs have no authentication, RBAC, or operator rate limit.
  They must not be exposed on the public internet in this state.
- Remote image checks resolve before `urllib` fetches, so DNS rebinding is not
  eliminated by a pinned connection. The per-operation socket timeout is also
  not a shared total request deadline across up to nine images.
- Catalog approval currently references mutable remote URLs. Production must
  ingest owned bytes into versioned object storage and bind activation to an
  immutable hash/manifest rather than trusting a URL to remain unchanged.
- FastAPI `BackgroundTasks` is not a durable worker and cannot resume after a
  process restart.
- Provider polling URLs are not persisted, so submitted jobs cannot yet be
  reconciled automatically.
- Narration and final media remain host-local rather than checksummed object
  storage artifacts.
- The quote does not cover script/TTS/render/storage and does not enforce an
  account-level hard budget after provider submission.
- Korean audio has duration fitting but no word-level forced-alignment proof.
- This review made no paid script, TTS, or video generation request.
