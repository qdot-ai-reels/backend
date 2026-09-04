# Studio production workflow contract

This document describes the implemented P0/P1 API boundary for the production
Studio. It deliberately distinguishes current behavior from the durable-worker
work that still has to be completed before multi-instance deployment.

## Implemented endpoints

All routes use the existing `/api/v1/reels` prefix.

### `GET/POST /prompt-versions` and `POST /prompt-versions/{id}/activate`

Prompt policy is stored as immutable bundles. Every bundle contains exactly six
templates: `script_generation`, `script_tts_repair`, `video_base`,
`video_identity_reference`, `video_generated_model`, and `creative_brief`.
Startup seeds and activates `production-v1` from the source-controlled files in
`app/prompt_defaults`. Concurrent application bootstrap is idempotent; the
singleton active pointer and append-only activation audit are committed in the
same transaction. If versions exist but the active pointer is missing or a
stored content hash fails verification, Studio fails closed instead of silently
selecting a prompt.

`GET /prompt-versions` returns:

```json
{
  "active_bundle_id": "production-v1",
  "versions": [
    {
      "id": "production-v1",
      "version": 1,
      "name": "Production v1",
      "description": "...",
      "content_sha256": "...",
      "created_at": "...",
      "activated_at": "...",
      "is_active": true,
      "templates": {"script_generation": "..."}
    }
  ]
}
```

`POST /prompt-versions` accepts `name`, `description`, and the complete
`templates` object and returns the newly saved version object with HTTP 201.
There is no edit or delete route. Activation returns the updated catalog.
These settings endpoints are the only public API that returns full template
text, and route responses use `Cache-Control: no-store`. They make no provider
request. If concurrent writers allocate the same next numeric version, the
losing save returns 409 `PROMPT_VERSION_CONFLICT` and can be retried safely.

Placeholders use `{{token}}`; whitespace such as `{{ product_context }}` is
also accepted. Missing required placeholders, per-template unknown tokens,
malformed braces, an individual template over 64 KiB, or a bundle over 256 KiB
returns 422. The required tokens are:

- `script_generation`: `product_context`, `creative_brief`,
  `template_scene_plan`
- `script_tts_repair`: `retry_error`
- `video_base`: `script_visual_table`
- `creative_brief`: `advertising_purpose`, `cta`, `visual_mode`
- identity-reference and generated-model templates have no required token

The runtime supplies every optional token allowed for each template. Studio
creative values are JSON-escaped and inserted in one render pass inside an
`untrusted-creative-brief-data` delimiter, so token-shaped user input is not
evaluated as template syntax. All model-facing instructions and delimiters live
in the six immutable templates; Python only serializes product fields, the
selected scene plan, and the script visual table into data placeholders. Prompt
instructions do not replace the existing code-level product, schema,
exact-timeline, dialogue-length, image, and final video validators.

### `GET /generation-templates`

Returns the four server-owned, versioned script strategies. The scene count,
order, section name and time range are immutable parts of a template version.

| Template | Timeline |
| --- | --- |
| `ugc_quick_4` v1 | Hook 0-1.2, Product 1.2-2.8, CTA 2.8-4 |
| `ugc_quick_6` v1 | Hook 0-1.5, Product 1.5-3.5, Lifestyle 3.5-4.8, CTA 4.8-6 |
| `ugc_balanced_8` v1 | Hook 0-2, Product 2-4.5, Lifestyle 4.5-6.5, CTA 6.5-8 |
| `ugc_full_15` v1 | Hook 0-3, Product 3-8, Lifestyle 8-12, CTA 12-15 |

Response shape:

```json
{
  "items": [
    {
      "id": "ugc_full_15",
      "version": 1,
      "name": "15초 풀 스토리",
      "description": "...",
      "duration_seconds": 15,
      "scene_plan": [
        {
          "key": "hook",
          "label": "Hook",
          "start_seconds": 0.0,
          "end_seconds": 3.0,
          "voiceover_max_syllables": 13
        }
      ]
    }
  ],
  "default_template_id": "ugc_full_15"
}
```

Template generation requires the exact duration supported by the selected
video model. Unlike the legacy maximum-duration script API, it never silently
downgrades a 15-second selection to a shorter provider duration.

### `POST /generation-quotes`

Persists an immutable quote for 15 minutes. The production output resolution is
currently restricted to `1080p`.

Before persistence, the endpoint reads the selected provider model catalog and
requires exact support for the template duration, `1080p`, and `9:16`. It does
not submit a video generation request. Catalog lookup failure returns the stable
`VIDEO_CATALOG_UNAVAILABLE` error with HTTP 502; an unsupported combination
returns `VIDEO_MODEL_UNSUPPORTED` with HTTP 422 and no quote is created.

```json
{
  "template_id": "ugc_full_15",
  "template_version": 1,
  "candidate_count": 1,
  "visual_mode": "generated_model",
  "resolution": "1080p",
  "prompt_version_id": "production-v1"
}
```

`prompt_version_id` is optional for older callers. When supplied, it must still
be the active version or the endpoint returns HTTP 409
`PROMPT_VERSION_CHANGED`. Every newly issued quote pins the active prompt
version ID, number, name, and content SHA-256 in both its request hash and its
public `prompt_version` metadata. Quotes created before this pin existed are not
safe for Studio generation and return `REQUOTE_REQUIRED`.

The default configured video rate is `$0.38 / second / candidate` and can be
changed with `VIDEO_RATE_PER_SECOND_USD`. The lower and upper range use
`VIDEO_QUOTE_MIN_FACTOR` (default `0.95`) and
`VIDEO_QUOTE_MAX_FACTOR` (default `1.10`). Values are stored with eight decimal
places, so a later settings change cannot rewrite an existing quote.

The response contains `line_items`, `total.min`, `total.expected`, `total.max`,
`created_at`, `expires_at`, and the selected model snapshot. `coverage` is
explicitly `video_only`: script generation, TTS, rendering, and a user-triggered
paid retry are not represented as if they were free or known. Automatic paid
video retry remains zero. `total.max` is the user-approved upper estimate for
that generation click and the configured rate card's upper value shown at
confirmation; it is not a provider-account hard spending cap.
Changing any quoted input requires a new quote.

### `POST /generate`

The existing product-and-script request remains valid. Studio can alternatively
send a template without a script:

```json
{
  "product": {"name": "상품", "image_url": "https://cdn.example/product.jpg"},
  "template_id": "ugc_full_15",
  "template_version": 1,
  "quote_id": "persisted-quote-id",
  "prompt_version_id": "production-v1",
  "client_request_id": "one-browser-submit-id",
  "candidate_count": 1,
  "visual_mode": "generated_model",
  "resolution": "1080p",
  "creative_brief": {
    "advertising_purpose": "상품 인지도",
    "cta": "링크 확인",
    "visual_mode": "generated_model",
    "channel": "Instagram Reels",
    "must_include": "상품 사용 장면",
    "must_exclude": "검증되지 않은 수량",
    "extra_details": "차분한 주방"
  }
}
```

Every request containing `template_id` must include both `quote_id` and
`client_request_id`. This makes the non-legacy Studio path explicitly budgeted
and idempotent. The legacy product-and-script request without `template_id`
remains valid and defaults to one candidate.

The nested `creative_brief` is authoritative for fields it contains. Existing
top-level controls remain accepted; if both shapes explicitly provide a field,
their values must match. This semantic check occurs after the durable request
reservation, so a mismatch is recorded as a definitive rejected request.
Non-empty legacy `prompt` text is rejected on the template path after the same
reservation; only the legacy non-template route may use free-form constraints
with the bundled prompt fallback.

The accepted combinations are:

- `script` without a template: legacy custom-script behavior.
- template without `script`: generate the constrained script asynchronously,
  then run TTS, video, audio merge, captions and final validation.
- template with `script`: validate the exact template scene layout before a job
  is created. A mismatch returns 422 before any paid video request.

When a template is selected, the model prompt contains the exact scene plan and
the backend reapplies and validates the canonical plan before TTS or captions.
The prompt also prohibits inventing package quantity, count, fine print, or
label numbers unless the product payload explicitly marks those claims as
verified.

`quote_id` must be unexpired and match template version, duration, candidate
count, visual mode, resolution, selected video model, and the requested prompt
version when present.
A stale or changed quote returns 409. A missing quote returns 404. The quoted
resolution snapshot is passed to the provider request instead of being replaced
by later runtime settings.

`client_request_id` provides body idempotency. Repeating the same canonical
request returns the first job with `idempotent_replay: true` and does not enqueue
another generation. Reusing the key for a changed body returns 409. The database
enforces a unique index in addition to the request-path lookup.

Clients may also send `Idempotency-Key`. When present it must exactly equal
`body.client_request_id`; a mismatch returns 422
`IDEMPOTENCY_KEY_MISMATCH` before reserving either contradictory key. Omitting
the header preserves the body-key contract. Schema-level Pydantic failures also
happen before the route can safely reserve a parsed body, so an uncertain
client must retry the exact same body and ID rather than minting a new one
solely because lookup is empty.

The quote is authoritative for prompt selection. On acceptance the private job
payload stores the exact six-template snapshot plus public prompt metadata.
Later activation cannot change a quoted job, TTS repair, or candidate retry.
Start, replay, list, and detail responses expose only `{id, version, name,
content_sha256}` and never template text. Script generation uses the pinned
`script_generation`; TTS overflow regeneration uses pinned
`script_tts_repair`; video generation selects the pinned base and visual-mode
template.

### `GET /generations`

Returns a newest-first management page. It accepts `limit` (1-100), opaque
`cursor`, and optional `status`.

Each item contains only a safe product snapshot, template, duration, visual
mode, candidate counts, estimate versus actual video cost, primary candidate
access URLs, error metadata, asset-fidelity warning, and timestamps. It never
returns `payload_json`, local file paths, provider polling URLs, or source image
reference arrays. Existing jobs without template or quote metadata remain
listable with nullable fields.

The existing `GET /generate/{job_id}` detail route retains its original fields
and adds safe `product`, `duration_seconds`, `template`, compact `quote`,
`cost_summary`, `asset_fidelity`, and allowlisted `options` values. Raw job and
candidate error evidence remains in the database, while list/detail responses
return stable public error codes and messages without provider detail or local
paths. A completed legacy row that only has the top-level output path is exposed
as one `legacy-primary` playable artifact.

Candidate retry is accepted only when the parent job is terminal, the candidate
is failed, and its public `retryable` value is exactly true. Provider timeout,
provider unavailability, shared script/TTS failures, and paid generation
failures are deliberately non-retryable: they require provider reconciliation,
whole-job recovery, or a new quote. A worker cannot race a retry while the
parent remains active.

## Current asynchronous boundary

The initial HTTP request returns 202 and work runs through the existing
`GenerationDispatcher` boundary. Job, payload, script, candidate state, quote,
cost, and output metadata are persisted. This is sufficient for the current
single-process local Studio and lets the frontend reload and continue polling.

It is not yet a durable queue. The current adapter is FastAPI
`BackgroundTasks`; a process restart can leave a `PENDING` or `PROCESSING` job
without an executing Python task. Provider `polling_url` is also not persisted,
and TTS/final artifacts are local files. Candidate records still live in one
JSON column, so PostgreSQL row locking serializes updates but does not provide a
normalized candidate audit trail.

The candidate retry implementation can resume only after shared narration has
been written locally. If script or TTS generation fails before
`runtime/tts/{job_id}/narration.mp3` exists, its public error is non-retryable
and candidate-only retry returns 409. The whole-job checkpoint recovery
described below is required for that case.

## Deferred production durability slice

Before multi-instance or zero-downtime production deployment:

1. Add Alembic migrations and normalized `generation_candidates` and
   `generation_tasks` tables.
2. Insert the job and durable outbox task in one transaction; claim work using
   a worker lease and PostgreSQL `FOR UPDATE SKIP LOCKED`.
3. Split provider submit and poll. Persist provider job ID and private polling
   URL immediately, then resume polling without issuing another paid POST after
   a crash.
4. Persist TTS, provider source, combined media, and final MP4 as checksummed S3
   artifacts rather than relying on one host's runtime directory.
5. Add a whole-job recovery endpoint that resumes the earliest incomplete
   checkpoint. Keep any new paid provider submission an explicit quoted action.
6. Add Korean word-level forced-alignment evidence. The current continuous TTS
   preserves natural prosody and fits the total voiced window, but it does not
   independently prove every internal phrase boundary against its caption.
7. Add authenticated operator authorization for prompt settings and owner-scope
   generation jobs/requests before exposing these routes on a public network.
8. Add retention/tombstone policy for prompt activation audits, generation
   request reservations, quotes, and private prompt snapshots.
9. Split the prompt settings catalog into paginated metadata and a single-version
   template detail endpoint before the number of 256 KiB bundles grows materially.

These deferred items are intentionally not described as completed by the P0/P1
contract.
