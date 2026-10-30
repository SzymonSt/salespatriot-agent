# Deploy to Google Cloud Run

For the teammate with access to the Google Cloud project. Takes about 15 minutes.

Two services, both built from this repository:

| Service | What | Who opens it | Image |
|---|---|---|---|
| `salespatriot-web` | `serve.py`: the web UI (`web/`), `/data/...`, live requests `/api/offers` with deal checks | **the jury** (public link) | `deploy/web.Dockerfile` |
| `salespatriot-api` | FastAPI + ADK agents (`app/backend`): `/v1/context`, `/v1/conversations`, `/v1/memory`, `/demo`, `/api/demo/run-live` | the team (private for now) | `deploy/api.Dockerfile` |

The web UI reads Supabase directly with the publishable key in `web/config.js` (read-only through RLS), so it
needs no secrets. Today the web does not call the API, so the API stays private (see step 7).

Both images were built and started locally on 2026-10-30:
- web: `/`, `/supplier/<id>`, `/data/profiles/*.json`, `/config.js`, `/logos/*` and `/api/offers` answer 200.
- api: `/health/live` 200, `/v1/profiles/summary` → 5 buyers and 20 suppliers, `/docs` and `/demo` 200, and 503 on
  database routes without `DATABASE_URL`. No `.env` is in the image, and it runs as a non-root user.

## 0. Prerequisites
- `gcloud` installed and logged in (`gcloud auth login`), with a project that has billing enabled.
- Roles on the project: Cloud Run Admin, Artifact Registry Admin, Cloud Build Editor, Secret Manager Admin and
  Service Account User. Owner or Editor also works.
- A **fresh clone of `main`**: `gcloud builds submit` uploads the local folder, including uncommitted files.
  ```bash
  git clone https://github.com/SzymonSt/salespatriot-agent.git && cd salespatriot-agent
  ```

## 1. Variables
```bash
export PROJECT_ID=<your-project-id>
export REGION=europe-west2          # London, next to the Supabase database (eu-west-2)
gcloud config set project "$PROJECT_ID"
```

## 2. Enable APIs and create the image repository
```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com
gcloud artifacts repositories create salespatriot --repository-format=docker --location="$REGION"
```

## 3. Secrets (API only)
Type the values yourself. Never put them in chat, the repository or the shell history of a shared machine.
```bash
printf '%s' '<GEMINI_API_KEY>' | gcloud secrets create google-api-key --data-file=-
printf '%s' '<DATABASE_URL>'   | gcloud secrets create database-url  --data-file=-
```
- `GEMINI_API_KEY` is the Gemini Developer API key (AI Studio). Check its quota first (docs/local-development.md).
- `DATABASE_URL`: Supabase → Connect → **Session pooler** (port 5432), for example
  `postgresql://postgres.aodxjmjnsqvadxcrepfr:<URL-encoded password>@<pooler-host>:5432/postgres`.
  The direct connection is IPv6-only and does not work from Cloud Run. If you don't know the password, reset it
  under Database → Settings.

Allow the Cloud Run runtime account to read the secrets:
```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
for s in google-api-key database-url; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

## 4. Build both images (Cloud Build, linux/amd64)
From the repository root:
```bash
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions _REGION="$REGION" .
```
To build on your own machine instead: `docker build --platform linux/amd64 -f deploy/web.Dockerfile ...`, then push.
On Apple Silicon the `--platform` flag is required.

## 5. Deploy the web UI (public)
```bash
gcloud run deploy salespatriot-web \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/salespatriot/web:latest" \
  --region "$REGION" --allow-unauthenticated --port 8080 \
  --cpu 1 --memory 1Gi --min-instances 1 --max-instances 1 --no-cpu-throttling --timeout 300
```
- `--max-instances 1`: live requests are kept in the container's memory and file system. One instance keeps them
  consistent during the demo. They are lost on restart, which is fine for a demo.
- `--min-instances 1` + `--no-cpu-throttling`: on start, `serve.py` warms up the deal-check engine in a background
  thread (~20 s: sanctions lists, TED history) and runs checks in the background. Without always-on CPU those
  threads stall between requests.

## 6. Check the web UI
- Open the service URL: the overview page, `/suppliers`, `/buyers` and a supplier page.
- Family and calibre chips appear when Supabase answers (ontology layer). `?no-ontology` shows the static-only view.
- Post a live request as a buyer and wait ~20–60 s: matched suppliers show it with their deal check.
- Append `?demo` to the URL to open the demo negotiations on load.
- **This URL is the link for the jury.**

## 7. Deploy the API (private for now)
```bash
gcloud run deploy salespatriot-api \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/salespatriot/api:latest" \
  --region "$REGION" --no-allow-unauthenticated --port 8080 \
  --cpu 1 --memory 2Gi --min-instances 0 --max-instances 2 --timeout 300 \
  --set-secrets GOOGLE_API_KEY=google-api-key:latest,DATABASE_URL=database-url:latest \
  --set-env-vars GEMINI_MODEL=gemini-2.5-flash
```
Test it with your own identity:
```bash
API_URL=$(gcloud run services describe salespatriot-api --region "$REGION" --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" "$API_URL/health/ready"        # {"status":"ready","database":"connected"}
curl -H "Authorization: Bearer $TOKEN" -X POST "$API_URL/v1/context" -H 'Content-Type: application/json' \
  -d '{"role":"buyer","purpose":"search","subject":{},"query":"9x19 ammunition","viewer_id":"<a buyer id>"}'
```
Its routes have no application-level auth yet. They write to the database and spend Gemini quota. Make the API
public only when the web UI starts calling it, and at the same time:
- add the API URL to `web/config.js` and allow the web origin in CORS on the API;
- add an access check (shared token or user sign-in);
- set a budget alert on the project.

Then: `gcloud run services add-iam-policy-binding salespatriot-api --region "$REGION" --member=allUsers --role=roles/run.invoker`.

## 8. Update after new commits
```bash
git pull
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions _REGION="$REGION" .
gcloud run deploy salespatriot-web --image "$REGION-docker.pkg.dev/$PROJECT_ID/salespatriot/web:latest" --region "$REGION"
gcloud run deploy salespatriot-api --image "$REGION-docker.pkg.dev/$PROJECT_ID/salespatriot/api:latest" --region "$REGION"
```
A new revision is created only by `deploy`. Pushing `:latest` alone does not update a running service.

## 9. After the hackathon
`--min-instances 1` with always-on CPU costs money every hour. Scale down or delete:
```bash
gcloud run services update salespatriot-web --region "$REGION" --min-instances 0
gcloud run services delete salespatriot-web salespatriot-api --region "$REGION"
```

## Known issues
- **`serve.py`, `log_message`:** when the server answers an error (404, or a path outside `web/`/`data/`), `args[0]`
  is an `HTTPStatus`, and `"/api/offers?" not in args[0]` raises `TypeError`. The client then gets a dropped
  connection instead of a 404. The data is safe; nothing leaks. Fix: use `str(args[0]) if args else ""`.
- Live requests are lost on a restart or redeploy of the web service (see step 5).
- Supabase TED data refreshes hourly (`ted-sync`); the UI reads it live. No redeploy is needed for new data.
