# Supabase: live TED sync and API

`[VERIFIED 2026-10-30]` Deployed to Supabase org **SalesPatriot**, project **hackathon**
(`aodxjmjnsqvadxcrepfr`, eu-west-2). Code: `../supabase/`.

## How it works
```
pg_cron "ted-sync-hourly" (minute 17 of every hour)
  → public.ted_sync_trigger('incremental', 2)          -- pg_net HTTP POST, secret from Vault
    → Edge Function ted-sync (Deno)                    -- checks the secret via public.ted_sync_check_secret
      → TED Search API (4 focus CPV classes, supplies, PD >= today(-2))
      → upsert public.ted_notices / public.ted_awards, log to public.ted_sync_runs
```
- Two queries: **competition** (tenders, the same notice types as `21-focus-scope.md`) and **result** (award notices).
- Incremental runs re-read the last 2 days, so overlaps are safe; rows are upserted by `publication_number`.
- **Backfill** (full history since 2016) is resumable. The cursor is kept in `ted_sync_state`; the full history takes ~10 s.
- The secret is generated inside the database and stored in Vault. It is never in git or in any client.

## Why pg_cron + Edge Function (free plan)
| Limit (free plan) | Value | Our use |
|---|---|---|
| Edge Function invocations | 500,000 / month | 24/day ≈ 720 / month |
| Wall clock per call | 150 s | Incremental ~2–5 s; full backfill ~10 s |
| CPU per call | 2 s (async I/O excluded) | JSON parse + map of ≤ 250 notices per page |
| Database | 500 MB | ~5k notices with `raw` jsonb, a few tens of MB |
| Inactive projects | Paused after 1 week | The hourly cron keeps activity going |

pg_cron alone cannot comfortably parse JSON from HTTP responses; the Edge Function does the logic,
and pg_cron only schedules it. Hourly is well within limits, so no weekly fallback is needed.

## API
**Status (public):** `GET https://aodxjmjnsqvadxcrepfr.supabase.co/functions/v1/ted-sync` returns
the counts (notices, awards, open tenders) and the last 5 runs.

**Data (public read, RLS: select only):** PostgREST with the publishable key (`apikey` header):
```
GET https://aodxjmjnsqvadxcrepfr.supabase.co/rest/v1/ted_open_tenders?select=*&order=deadline_tender.asc
GET .../rest/v1/ted_notices?kind=eq.competition&focus_classes=cs.{ammunition}&order=publication_date.desc&limit=50
GET .../rest/v1/ted_awards?winner_country=eq.POL&order=decision_date.desc.nullslast
GET .../rest/v1/ted_notices?select=publication_number,title_en,ted_awards(winner_name,winner_identifier)&kind=eq.result
```
Or with supabase-js: `supabase.from('ted_open_tenders').select('*')`.

**Run a sync now (SQL editor or MCP, as the database owner):**
```sql
select public.ted_sync_trigger('incremental', 7);   -- last 7 days
select public.ted_sync_trigger('backfill');         -- resume or finish the full history
select * from public.ted_sync_runs order by id desc limit 5;
```

## Tables
| Table / view | Contents |
|---|---|
| `ted_notices` | One row per notice: kind (`competition` or `result`), type, date, legal basis, procedure, `title_en`, original title and description, buyer (name, identifier, country, legal type, main activity), `cpv_codes`, `focus_classes` (ammunition, firearms, parts, miscellaneous_weapons; empty = noise such as 35341100), estimated value, deadlines, result value, bids received, `ted_url`, `raw` (all fetched fields) |
| `ted_awards` | One row per winner of a result notice: name, identifier (free text), country, size, owner nationality, decision date, notice total value, `co_winners` |
| `ted_open_tenders` | View: competition notices in focus whose tender or request deadline is still ahead |
| `ted_sync_runs` | Log of every run |
| `ted_sync_state` | Backfill cursor (not public) |

## Status on 2026-10-30
Backfill finished in 35 s: **4,840 notices** (2,230 competition + 2,610 result), **1,036 winner rows**,
**36 open tenders** (the earlier count of 38 included 35341100 plumbing noise and tenders whose deadline had passed). Incremental
runs: 10 notices, 6 winners, ~2 s. Security advisors: `pg_net` moved to schema `extensions`. `ted_sync_state` has RLS
with no policy on purpose (service role only).

## Known data limits
- **Winners exist only for eForms notices (from late 2023).** Older award notices in the Search
  API carry only `total-value`, so roughly 1,036 winner rows come out of ~2,610 award notices.
  Older winners would need the legacy XML ([24](24-ted-notice-format.md)).
- `notice_value` is the notice total. For multi-lot notices, the split per winner needs the eForms XML.
- Identifiers are free text. Resolve them into canonical companies next ([19](19-ted-epo-ontology.md), PLAN step 3).

## Redeploy
- Migrations: `supabase/migrations/` (apply in order).
- Function: deploy `supabase/functions/ted-sync` with `verify_jwt = false`, because the function checks the Vault secret itself.
- After setting up a new project, run the two `vault.create_secret` lines from the cron migration header.
