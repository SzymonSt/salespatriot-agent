# Backend API reference

The API is implemented in `app/backend/main.py`; the synthetic browser demo
routes are in `app/backend/demo.py`. FastAPI also exposes interactive OpenAPI
documentation at `/docs` while the server is running.

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
  "caveats": ["Availability needs confirmation."]
}
```

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
  "message": "Please provide an indicative quote for the requirement."
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

The response contains a `run_id`, `demo_only`, `persisted`,
`external_messages_sent`, model name, synthetic demand, ordered timeline,
buyer quote-request text, illustrative quotes, selected quote, and disclaimer.
On success, `persisted` and `external_messages_sent` are `false`.

## Error responses

FastAPI error responses use a JSON `detail` field.

| Status | Meaning |
|---|---|
| `403` | Buyer requested an action not allowed by its active policy. |
| `404` | Requested buyer or supplier profile was not loaded. |
| `409` | Supplier is not enabled/opted in, or buyer has no active policy. |
| `422` | Request body failed schema validation. |
| `502` | ADK/Gemini invocation failed or returned an unauthorized/invalid result. |
| `503` | Required Gemini key or usable database configuration is missing, or readiness probe failed. |

The demo includes a sanitized Gemini provider message in its `502` detail to
help troubleshoot local runs. Do not copy secrets into support messages.
