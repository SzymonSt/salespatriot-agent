# Local setup, demo, and Gemini API quotas

## Run locally

From the repository root:

```bash
uv sync --project app/backend --extra dev
uv run --project app/backend uvicorn app.backend.main:app --reload --host 127.0.0.1
```

Create `app/backend/.env` from the tracked `app/backend/.env.example` and set:

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Gemini Developer API key for the Google Cloud project used by the backend. |
| `GEMINI_MODEL` | Model ID passed to ADK; defaults to `gemini-2.5-flash` when omitted. |
| `DATABASE_URL` | Optional Supabase/PostgreSQL connection string used by the readiness probe. |

Keep `.env` local and never commit or share its contents. The service is
unauthenticated in this development slice; keep it bound to `127.0.0.1`.

## Run the synthetic demo

Open <http://127.0.0.1:8000/demo> and click **Run demo**. A successful flow
makes three Gemini API calls and may incur charges. It uses synthetic data,
saves nothing to Supabase, sends no external messages, submits no bid, and
makes no binding offer.

The demonstration includes a seeded simulated peer-buyer reference and
illustrative quote fixtures. It is not a live procurement integration or a
test of real-world due diligence.

## Gemini API subscription and billing are separate

A Google AI Pro subscription primarily expands access in the Gemini app and
other covered Google products. It does not automatically change the billing
tier of the Google Cloud project associated with a Gemini Developer API key.
Gemini API quotas are tied to the project, not the individual key.

For the API, verify the project and its active limits in [AI Studio's rate-limit
dashboard](https://aistudio.google.com/rate-limit?timeRange=last-28-days). If
the project needs paid API access, link an active billing account to that same
Google Cloud project using Google's [Gemini API billing
guide](https://ai.google.dev/gemini-api/docs/billing). Review Google's current
[API pricing](https://ai.google.dev/gemini-api/docs/pricing) before enabling
paid usage. Preview models can have more restricted limits.

For example, `429 RESOURCE_EXHAUSTED` with a
`free_tier_input_token_count` limit of `0` for `gemini-3.1-pro` means that the
project's Free API tier has no input-token allowance for that model. It does
not mean that a Google AI Pro app subscription has been consumed. Billing must
be enabled for the API project, and the model's paid-tier limits must allow the
request.

## Common Gemini errors

| Error | Likely explanation and next step |
|---|---|
| `400 INVALID_ARGUMENT` | The provider rejected a request field or schema. Read the sanitized detail; the output schema must follow the Gemini endpoint's supported format. |
| `404 NOT_FOUND` | The model ID may be retired or unavailable to the API project. Select a model currently available to the project. |
| `429 RESOURCE_EXHAUSTED` | The project's model-specific quota or rate limit was exceeded or is zero for its current tier. Check the AI Studio rate-limit dashboard and billing project. |

## Database readiness

`GET /health/live` checks only that the process is running. `GET /health/ready`
performs a read-only `SELECT 1` against Supabase/PostgreSQL. The current backend
does not create tables, run migrations, or persist the demo or agent
conversations.

Run the backend regression suite with:

```bash
uv run --project app/backend python -m pytest app/backend/tests
```
