# Backend API reference

The API is implemented in `app/backend/main.py`; the synthetic browser demo
routes are in `app/backend/demo.py`; the context, knowledge, memory and
conversation routes are in `app/backend/knowledge/routes.py`. FastAPI also
exposes interactive OpenAPI documentation at `/docs` while the server is running.

## Access and conventions

The current preview routes are unauthenticated. Run the server bound to
`127.0.0.1`; do not expose it publicly. JSON request bodies use
`Content-Type: application/json`. Unknown request fields are rejected.

## Health and profile routes

| Method and path | Purpose | Success response |
|---|---|---|
| `GET /health/live` | Process liveness; does not check dependencies. | `{"status":"alive"}` |
| `GET /health/ready` | Read-only database connectivity probe. | `{"status":"ready","database":"connected"}` |
| `GET /v1/profiles/summary` | Counts profiles loaded in memory. | `{"buyers": <count>, "suppliers": <count>}` |

`/health/ready` returns `503` if `DATABASE_URL` is absent, is a placeholder, or
the PostgreSQL check fails. It does not verify migrations or application data.

## Supplier introduction preview

`POST /v1/agents/suppliers/{profile_id}/preview`

The profile must exist, have `agent_settings.enabled: true`, and have at least
one `opted_in_categories` entry matching a submitted CPV code.

Request:

```json
{
  "demand_id": "notice-123",
  "title": "Example equipment",
  "description": "Short notice description",
  "cpv_codes": ["35110000"],
  "quantity": "20 units",
  "deadline": "2027-01-30"
}
```

`demand_id` and `title` are required. The other fields are optional; `description`
and `cpv_codes` default to empty values.

Response:

```json
{
  "non_binding_message": "We are interested in discussing this requirement.",
  "caveats": ["Availability needs confirmation."],
  "refs": []
}
```

`refs` lists cited knowledge refs. The preview routes give the agent no tools, so
it is normally empty; it is filled in persisted conversations.

The request passes the supplier's profile data and capacity declarations to
the model. It does not query verified stock or reserve inventory.

## Buyer communication draft

`POST /v1/agents/buyers/{profile_id}/draft-message`

The buyer must have an active `agent_policy`, and `action` must appear in that
policy's `permitted_actions`.

Request:

```json
{
  "action": "request_quote",
  "decision": "CLEARED_TO_REQUEST_QUOTE",
  "evidence_summary": "The caller's checks permit an indicative quote request.",
  "permitted_data": ["notice title", "quantity", "delivery window"]
}
```

Allowed `action` values are `request_quote`, `request_clarification`, `decline`,
and `escalate`. Allowed `decision` values are `GO`, `GO_WITH_CONDITIONS`,
`ESCALATE`, `NO_GO`, and `CLEARED_TO_REQUEST_QUOTE`. `permitted_data` defaults
to an empty list.

Response:

```json
{
  "action": "request_quote",
  "message": "Please provide an indicative quote for the requirement.",
  "refs": []
}
```

The API verifies that the generated action matches the authorized action. The
caller supplies the decision, evidence summary, and permitted data; this route
does not run due-diligence checks itself.

## Manual demo routes

### Profile-backed buyer-agent reference exchange

`GET /v1/agents/reference-options` returns loaded buyer agents and suppliers
for whom at least one buyer profile contains a matching public counterpart
record. Options are built from the current `data/profiles/` files at startup.

`POST /v1/agents/buyers/{requester_buyer_id}/reference-requests`

Request:

```json
{
  "supplier_id": "supplier-svn-2237741000",
  "peer_buyer_id": "buyer-svn-5268923000"
}
```

This example has Austria's buyer profile request a public-profile reference
from Slovenia's buyer profile about PANNA PLUS. The backend routes a typed
message between two in-process `BuyerProfileAgent` instances. The peer agent
responds only from matching `top_counterparts` and `recent_notices` fields in
its own profile.

The response contains the request and response envelopes, the allowlisted
evidence, and these safety indicators:

```json
{
  "data_scope": "public_ted_profile_allowlist",
  "private_profile_data_shared": false,
  "external_organization_contacted": false,
  "persisted": false
}
```

The evidence may establish a public procurement relationship, but it does not
establish delivery quality or satisfaction. The endpoint does not claim that a
real public authority replied; no Gemini call or external network message is
made. It returns `400` for self-requests, `404` for unknown profile IDs, and
`409` when the selected peer has no matching public evidence.

### Synthetic demo

| Method and path | Purpose |
|---|---|
| `GET /demo` | Serves the local synthetic demo page. |
| `POST /api/demo/run` | Runs the synthetic supplier/buyer flow and returns `DemoRunResult`. |
| `POST /api/demo/run-live` | Not synthetic: runs the persisted flow (`demo = true`) on a real open TED tender. See [Persisted demo on a real tender](#persisted-demo-on-a-real-tender). |

The `/api/demo/run` response contains a `run_id`, `demo_only`, `persisted`,
`external_messages_sent`, model name, synthetic demand, ordered timeline,
buyer quote-request text, illustrative quotes, selected quote, and disclaimer.
On success, `persisted` and `external_messages_sent` are `false`. This route is
unchanged and still uses synthetic fixtures only.

## Context, knowledge and memory routes

These routes implement §3.6 of
[agent-context-rag-implementation.md](./agent-context-rag-implementation.md).
They call the Supabase RPCs through `DATABASE_URL`, which must use a role that
may execute them (the `postgres` pooler user or the service role). Without a
usable `DATABASE_URL` every route here returns `503`. `viewer_id` is asserted by
the caller: the routes are local and unauthenticated like the rest of the API.

When a request has a `query`, the backend embeds it with `gemini-embedding-001`
(768 dimensions, `RETRIEVAL_QUERY`, L2-normalised, 3 s timeout, cached). If the
embedding fails or `GOOGLE_API_KEY` is missing, the RPC gets no vector and falls
back to full-text search. The response header `X-Query-Embedding` says `used`,
`unavailable` or `none`.

| Method and path | Body | Calls | Response |
|---|---|---|---|
| `POST /v1/context` | `{role, purpose, subject, query?, viewer_id, limit?}` | `agent_context` | the context pack (§3.4) |
| `POST /v1/knowledge/search` | `{query, kinds?, family_id?, calibre?, cpv_prefix?, country?, viewer_id, limit?}` | `kb_search` | `{query, hits: [{ref, kind, title, snippet, facts, score}]}` |
| `POST /v1/memory/recall` | `{query, role, family_id?, country?, counterparty?, viewer_id, limit?}` | `memory_recall` | `{memories: [...]}` |
| `POST /v1/memory` | memory entry (below) | `memory_write` | `201 {id, ref, status}` |
| `POST /v1/memory/{id}/feedback` | `{helpful: bool}` | `memory_feedback` | `{ref, helpful, unhelpful}`; `404` if unknown |

`role` is `buyer` or `supplier`. `purpose` and its required `subject` keys:

| `purpose` | Required `subject` |
|---|---|
| `assess_tender` | `publication_number`, `supplier_id` |
| `find_suppliers` | `publication_number`, or `family_id` (optionally `calibre`, `country`) |
| `evaluate_quote` | `conversation_id`, `message_id` |
| `check_counterparty` | `supplier_id` or `buyer_id` |
| `search` | none, but `query` is required |

Other `subject` keys are rejected. `limit` is 1–20 (default 8; 5 for recall).

Memory entries written through the API are always agent entries with status
`proposed`: `kind` is `episode` (needs `conversation_id`), `preference` or
`pattern_proposal`; `visibility` is `owner` (default) or `parties`. Lessons,
validated entries and `public` visibility are reserved for service-role jobs:

```json
{
  "kind": "preference",
  "role": "buyer",
  "owner_id": "buyer-deu-991-19518-88",
  "text": "Prefers DAP delivery for small-calibre ammunition",
  "family_id": "ammunition.small_calibre",
  "visibility": "parties",
  "party_ids": ["supplier-cze-65414128"],
  "evidence_refs": ["conv:<uuid>"]
}
```

## Conversation routes

| Method and path | Body | What happens |
|---|---|---|
| `POST /v1/conversations` | `{publication_number, buyer_id, supplier_id, buyer_mandate?, supplier_mandate?, demo, run?}` | inserts the conversation, runs the supplier introduction turn; with `run: "full"` runs the whole flow. `201` |
| `POST /v1/conversations/{id}/messages` | `{sender_role, sender_id, type, text?, payload?, refs?}` | appends through `agent_message_append`, then runs the counterpart's turn |
| `GET /v1/conversations/{id}` | none | `{conversation, messages, tool_calls, outcome}` |
| `POST /v1/conversations/{id}/outcome` | `{delivered_on_time?, quality?, final_price?, unit_price?, quantity?, unit?, currency?, notes?, reported_by}` | `agent_conversation_close`: upserts `deal_outcomes`, writes the episode memory, refreshes the conversation chunk, closes if still open |

`POST /v1/conversations` and posted messages that start an agent turn need
`GOOGLE_API_KEY` (`503` otherwise). The tender must be open in
`ted_open_tenders` (`404` otherwise). With `demo: false` the supplier must have
`agent_settings.enabled` with a matching CPV category, and the buyer an active
policy that permits `request_quote` (`409` otherwise), as for the preview routes.

Counterpart turns for posted messages:

| Posted message | Agent turn |
|---|---|
| buyer `rfq` or `counter` | supplier quote (checked against the supplier mandate) |
| supplier `quote` (needs `payload.unit_price`, `payload.currency`, `payload.quantity`) | buyer evaluation: `accept`, `counter` or `escalate` |
| `accept` by either side | close with the accepted terms (a supplier accepting a buyer counter closes at the counter price) |
| anything else | none; the message is stored |

`sender_id` must be the conversation's buyer or supplier for that role (`403`).
Which `type` each side may send is validated (`422`); the order is enforced by the
database state machine (`409`, e.g. `accept` before a quote).

A mandate (both sides; omitted → active `agent_mandates` row for the company,
else the default platform policy of `deal_due_diligence.py`):

```json
{
  "max_autonomous_value_eur": 250000,
  "min_supplier_score": 6.0,
  "allowed_kyc_status": ["verified", "limited"],
  "allowed_cpv_prefixes": ["3531", "3532", "3533", "3534"],
  "max_rounds": 2,
  "price_ceiling": 50000,
  "min_unit_price": null,
  "currency": "EUR",
  "min_evidence_refs": 1
}
```

`price_ceiling` (buyer, total) and `min_unit_price` (supplier) need `currency`.
The run's response is `{conversation_id, status, demo, steps, conversation}`;
`conversation` has the same shape as `GET /v1/conversations/{id}`.

### Persisted demo on a real tender

`POST /api/demo/run-live` takes an optional body
`{publication_number?, supplier_id?, buyer_mandate?, supplier_mandate?}`. Without
a body it picks the open TED tender with an assigned product family and a matched
profiled supplier (EUR tenders first, earliest deadline), uses the linked
`buyers.id` or `buyer:<buyer name>`, and runs the full flow with `demo = true`.
The response adds `tender`, `buyer_is_simulated: true` and a `disclaimer`. The
buyer agent simulates the contracting authority; nothing is sent to anyone and
quotes are indicative and non-binding. A full run makes 4 to 8 Gemini calls (more
when the agents use tools) and writes rows to Supabase.

## Error responses

FastAPI error responses use a JSON `detail` field.

| Status | Meaning |
|---|---|
| `400` | A buyer agent requested a peer reference from itself. |
| `403` | Buyer requested an action not allowed by its active policy, or a message `sender_id` is not that party of the conversation. |
| `404` | Requested profile was not loaded; conversation, memory entry or open tender not found. |
| `409` | Supplier is not enabled/opted in, buyer has no active policy, the selected peer has no matching public evidence, or the database rejected a state transition. |
| `422` | Request body failed schema validation, or the database rejected a parameter value. |
| `502` | ADK/Gemini invocation failed or returned an unauthorized/invalid result. In a conversation, the failure is also stored as a system `escalate` message. |
| `503` | Required Gemini key or usable database configuration is missing, the readiness probe failed, or an RPC is not deployed yet. |

The demo includes a sanitized Gemini provider message in its `502` detail to
help troubleshoot local runs. Do not copy secrets into support messages.
