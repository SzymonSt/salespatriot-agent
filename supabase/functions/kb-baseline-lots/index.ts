// Edge Function: GET = progress of the lot pass (public counts), POST = run ~budgetSeconds of it (needs
// x-baseline-secret from Vault, same secret and pattern as kb-baseline-harvest). Fetches the TED result-notice XML
// listed in kb_snapshots.harvest_state.lots.sample (1 request/s), parses lots in memory (twin of
// ted_baseline_lots.py) and upserts kb_baseline_lots. Raw XML is never stored.
// Deploy with verify_jwt = false: the function checks the Vault secret itself.
import { checkSecret, redact, runLots, status } from "./lots.ts";

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

function json(body: unknown, code = 200) {
  return new Response(JSON.stringify(body, null, 2), { status: code, headers: { "Content-Type": "application/json" } });
}

const snapshotId = (value: unknown) => (typeof value === "string" && /^[a-z0-9_]{1,40}$/.test(value) ? value : "baseline_v2");

Deno.serve(async (req) => {
  try {
    if (req.method === "GET") return json(await status(db, snapshotId(new URL(req.url).searchParams.get("snapshot"))));
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);
    if (!(await checkSecret(db, req.headers.get("x-baseline-secret")))) return json({ error: "unauthorized" }, 401);
    const body = await req.json().catch(() => ({}));
    const budget = Math.min(Math.max(Number(body.budgetSeconds) || 100, 10), 120);
    return json(await runLots(db, snapshotId(body.snapshot), budget));
  } catch (error) {
    return json({ error: redact(String(error)) }, 500);
  }
});
