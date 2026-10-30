// Edge Function: GET = embedding status (public), POST = embed pending chunks and memory (needs x-sync-secret
// from Vault). Scheduled every 5 minutes by pg_cron + pg_net (supabase/migrations/*_kb_embed_rpc.sql).
// Gemini key: Edge Function secret GOOGLE_API_KEY (set in the dashboard, never in git). Its value is never
// logged or returned: errors pass through redactor() first.
import { checkSecret, readApiKey, redactor, run, status } from "./embed.ts";

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

const clamp = (value: unknown, min: number, max: number, fallback: number) =>
  Math.min(Math.max(Number(value) || fallback, min), max);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: cors });
  const apiKey = readApiKey(Deno.env.get("GOOGLE_API_KEY"));
  const redact = redactor(apiKey);
  try {
    if (req.method === "GET") return json(await status(db, apiKey));
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

    if (!(await checkSecret(db, req.headers.get("x-sync-secret")))) return json({ error: "unauthorized" }, 401);
    if (apiKey.state === "missing") return json({ status: "missing GOOGLE_API_KEY" });
    if (apiKey.state === "invalid format") {
      return json({ status: "invalid GOOGLE_API_KEY: store only the bare key (one line, no spaces)" });
    }

    const body = await req.json().catch(() => ({}));
    return json(
      await run(db, apiKey, {
        mode: String(body.mode ?? "manual").slice(0, 40),
        budgetSeconds: clamp(body.budgetSeconds, 5, 120, 100),
        batchSize: clamp(body.batchSize, 1, 100, 100),
      }),
    );
  } catch (error) {
    return json({ error: redact(String(error)) }, 500);
  }
});
