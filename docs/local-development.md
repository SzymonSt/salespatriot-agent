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

## Run profile-backed buyer-agent references

Open <http://127.0.0.1:8000/demo> and use the **Profile-backed buyer-agent
reference check** section. The options come from actual buyer and supplier
profiles in `data/profiles/`. A buyer agent can ask another buyer agent for
public TED counterpart/notice evidence about a supplier.

This exchange is mediated in-process by the backend. It sends no external
message and makes no Gemini API call. Only public allowlisted counterpart and
notice fields are returned; KYC and due-diligence profile fields are excluded.
The source profiles do not contain delivery or quality assessments, so the
agent reports that limitation rather than presenting a public award as a
positive performance reference. These are platform-hosted profile agents, not
agents operated by the named buyers.

## Run the synthetic ADK demo

The same page also has a **Run synthetic demo** button. It uses synthetic
suppliers, diligence results, a simulated peer reference, and illustrative
quotes. A successful run makes up to three Gemini API calls and may incur
charges. It sends no external messages and persists nothing to Supabase.

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
