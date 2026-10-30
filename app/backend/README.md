# SalesPatriot backend

Architecture, API, and local-development documentation is indexed in
[`docs/README.md`](../../docs/README.md).

The backend loads buyer and supplier profiles from `data/profiles/*.json`
at startup. It does not write those profiles to PostgreSQL.

## Local setup

Use Python 3.11 or later and `uv`. From the repository root:

```bash
uv sync --project app/backend --extra dev
```

Add these values to the existing `app/backend/.env` file:

- `GOOGLE_API_KEY`: Gemini Developer API key from Google AI Studio.
- `GEMINI_MODEL`: `gemini-3.8-flash` (the default; `gemini-2.5-flash` is no longer available to new API keys).
- `DATABASE_URL`: the Supabase session-pooler URI, with the database password URL-encoded.

Do not commit `app/backend/.env` or send its contents through chat. The tracked
`app/backend/.env.example` contains placeholders only.

Start the API from the repository root:

```bash
uv run --project app/backend uvicorn app.backend.main:app --reload --host 127.0.0.1
```

Agent preview routes are unauthenticated in this local-development slice.
Keep the server bound to `127.0.0.1`; do not expose it publicly until proper
organization authentication is implemented.

`GET /health/live` checks that the process is running. `GET /health/ready`
performs a read-only `SELECT 1` against Supabase and returns `503` until a
usable `DATABASE_URL` is configured.

## Manual A2A flow demo

Open `http://127.0.0.1:8000/demo` for two manual flows. The profile-backed
reference check routes a public TED evidence request between in-process buyer
profile agents using real records from `data/profiles/`; it makes no Gemini
call and contacts no real organization. The separate synthetic ADK flow calls
two supplier agents and one buyer agent, then displays simulated references,
due-diligence checks, quote fixtures, and deterministic quote comparison.

The synthetic flow uses non-controlled goods, organizations, references, and
quotes. It sends no external messages, submits no bid, and does not persist to
Supabase. It makes up to three Gemini API calls and may incur API usage charges.

## Context, RAG, memory and persisted conversations

`app/backend/knowledge/` adds the context, knowledge, memory and conversation
routes of `docs/agent-context-rag-implementation.md` §3.6, the ADK knowledge
tools, the conversation orchestrator and `POST /api/demo/run-live`. They need
`DATABASE_URL` (and `GOOGLE_API_KEY` for agent turns). See
[`docs/agent-workflow.md`](../../docs/agent-workflow.md) and
[`docs/backend-api.md`](../../docs/backend-api.md). Load the knowledge base into
the RAG store with:

```bash
uv run --project app/backend python -m app.backend.knowledge.load_kb_docs
```

## Agent previews

The supplier preview endpoint requires an explicit `agent_settings.enabled`
flag and a matching opted-in CPV category in the supplier profile. The buyer
draft endpoint requires an `agent_policy` with `status: "active"` and an
explicitly permitted action. Existing generated profiles do not have these
permissions, so the endpoints reject them by default.

These endpoints call ADK/Gemini to produce structured previews only. They do
not send messages to buyers, submit bids, make binding offers, or persist
conversations. The policy and opt-in fields must be explicitly configured
before previews are enabled for a profile.

Run backend tests:

```bash
uv run --project app/backend python -m pytest app/backend/tests
```
