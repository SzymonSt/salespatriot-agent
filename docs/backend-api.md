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
