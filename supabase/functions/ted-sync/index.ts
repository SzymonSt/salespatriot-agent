// Edge Function: GET = sync status (public), POST = run a sync (needs x-sync-secret from Vault).
// Scheduled hourly by pg_cron + pg_net (supabase/migrations/*_ted_sync_cron.sql).
import { checkSecret, runBackfill, runIncremental, status } from "./sync.ts";

function serviceKey(): string {
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (legacy) return legacy;
  const keys = Deno.env.get("SUPABASE_SECRET_KEYS") ?? "";
  try {
    return Object.values(JSON.parse(keys) as Record<string, string>)[0];
  } catch {
    return keys;
  }
}

const db = { url: Deno.env.get("SUPABASE_URL")!, key: serviceKey() };
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, x-sync-secret, authorization, apikey",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "Content-Type": "application/json", ...cors },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: cors });
  try {
    if (req.method === "GET") return json(await status(db));
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

    if (!(await checkSecret(db, req.headers.get("x-sync-secret")))) return json({ error: "unauthorized" }, 401);

    const body = await req.json().catch(() => ({}));
    if (body.mode === "backfill") {
      return json(await runBackfill(db, Math.min(Number(body.budgetSeconds) || 100, 120), body.restart === true));
    }
    return json(await runIncremental(db, Math.min(Math.max(Number(body.days) || 2, 1), 30)));
  } catch (error) {
    return json({ error: String(error) }, 500);
  }
});
