// Edge Function: GET = harvest status (public), POST = run ~budgetSeconds of the baseline harvest
// (needs x-baseline-secret from Vault). Triggered by public.kb_baseline_trigger() until harvest_state.done.
// Deploy with verify_jwt = false: the function checks the Vault secret itself (same pattern as ted-sync).
import { checkSecret, runHarvest, status } from "./harvest.ts";

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
  "Access-Control-Allow-Headers": "content-type, x-baseline-secret, authorization, apikey",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "Content-Type": "application/json", ...cors },
  });
}

const snapshotId = (value: unknown) => (typeof value === "string" && /^[a-z0-9_]{1,40}$/.test(value) ? value : "baseline_v1");

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: cors });
  try {
    if (req.method === "GET") return json(await status(db, snapshotId(new URL(req.url).searchParams.get("snapshot"))));
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

    if (!(await checkSecret(db, req.headers.get("x-baseline-secret")))) return json({ error: "unauthorized" }, 401);

    const body = await req.json().catch(() => ({}));
    const budget = Math.min(Math.max(Number(body.budgetSeconds) || 100, 10), 110);
    return json(await runHarvest(db, snapshotId(body.snapshot), budget, body.restart === true));
  } catch (error) {
    return json({ error: String(error) }, 500);
  }
});
