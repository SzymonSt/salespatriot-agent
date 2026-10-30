// Embeds pending RAG chunks (public.kb_chunks) and memory rows (public.agent_memory) with Gemini.
// Contract: docs/agent-context-rag-implementation.md §3.1 and §5 WS2.
// Plain TypeScript + fetch, so it runs in Deno (Edge Function) and in Node (local tests).

export const MODEL = "gemini-embedding-001";
export const DIM = 768;
export const MODEL_TAG = `${MODEL}@${DIM}`;
const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:batchEmbedContents`;
const MAX_BATCH = 100;
const MAX_CHARS = 8000; // ~2,000 tokens, the model's input limit; kb_chunks content is <= 2,000 chars anyway

export type Db = { url: string; key: string };
type Row = Record<string, unknown> & { id: string; content_hash: string | null };
type Source = {
  name: "kb_chunks" | "agent_memory";
  counter: "embedded" | "memory_embedded";
  queue: string;
  store: string;
  select: string;
  text: (row: Row) => string;
  title: (row: Row) => string | undefined;
};

// kb_embed_queue / kb_embed_store are ours (WS2); memory_embed_queue / memory_embed_store belong to WS4.
const SOURCES: Source[] = [
  {
    name: "kb_chunks",
    counter: "embedded",
    queue: "kb_embed_queue",
    store: "kb_embed_store",
    select: "id,title,content,content_hash",
    text: (r) => String(r.content ?? ""),
    title: (r) => (r.title ? String(r.title) : undefined),
  },
  {
    name: "agent_memory",
    counter: "memory_embedded",
    queue: "memory_embed_queue",
    store: "memory_embed_store",
    select: "id,kind,text,content_hash",
    text: (r) => String(r.text ?? ""),
    title: () => undefined,
  },
];

// ---------- Supabase REST (PostgREST), same helpers as ted-sync ----------

function headers(db: Db, extra: Record<string, string> = {}): Record<string, string> {
  // Legacy service_role keys are JWTs and go in Authorization too; sb_secret_ keys only in apikey.
  const auth: Record<string, string> = db.key.startsWith("eyJ") ? { Authorization: `Bearer ${db.key}` } : {};
  return { apikey: db.key, ...auth, "Content-Type": "application/json", ...extra };
}

class MissingRpc extends Error {}

async function rpc<T>(db: Db, fn: string, args: Record<string, unknown>, query = ""): Promise<T> {
  const response = await fetch(`${db.url}/rest/v1/rpc/${fn}${query}`, {
    method: "POST",
    headers: headers(db),
    body: JSON.stringify(args),
  });
  if (!response.ok) {
    const text = (await response.text()).slice(0, 300);
    // PGRST202 = function not found (e.g. WS4's memory RPCs not deployed yet).
    if (response.status === 404 && text.includes("PGRST202")) throw new MissingRpc(fn);
    throw new Error(`rpc ${fn} ${response.status}: ${text}`);
  }
  return response.json();
}

// The cron secret lives only in Vault; public.kb_embed_check_secret is callable by service_role only.
export async function checkSecret(db: Db, candidate: string | null): Promise<boolean> {
  if (!candidate) return false;
  try {
    return (await rpc<boolean>(db, "kb_embed_check_secret", { candidate })) === true;
  } catch {
    return false;
  }
}

export async function status(db: Db, keyConfigured: boolean) {
  return { google_api_key: keyConfigured ? "set" : "missing", ...(await rpc<object>(db, "kb_embed_status", {})) };
}

// ---------- Gemini ----------

type Item = { text: string; title?: string };

// Current REST shape: per-request embedContentConfig {taskType, title, outputDimensionality}. The older top-level
// fields (taskType, title, outputDimensionality) are deprecated but still documented; used as a fallback on 400.
function requestBody(items: Item[], legacy: boolean) {
  return {
    requests: items.map((item) => {
      const config: Record<string, unknown> = { taskType: "RETRIEVAL_DOCUMENT", outputDimensionality: DIM };
      if (item.title) config.title = item.title;
      const content = { parts: [{ text: item.text }] };
      return legacy
        ? { model: `models/${MODEL}`, content, ...config }
        : { model: `models/${MODEL}`, content, embedContentConfig: config };
    }),
  };
}

async function callGemini(apiKey: string, items: Item[], legacy: boolean) {
  return await fetch(GEMINI_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
    body: JSON.stringify(requestBody(items, legacy)),
  });
}

// Reduced-dimension vectors are not unit length: truncate to DIM (Matryoshka) if the API ignored the setting,
// then L2-normalise. Rounded to float32 precision to keep the payload small.
export function normalise(values: number[]): number[] {
  if (!Array.isArray(values) || values.length < DIM) throw new Error(`expected ${DIM} values, got ${values?.length}`);
  const v = values.length > DIM ? values.slice(0, DIM) : values;
  const norm = Math.sqrt(v.reduce((sum, x) => sum + x * x, 0));
  if (!Number.isFinite(norm) || norm === 0) throw new Error("zero or invalid vector");
  return v.map((x) => Number((x / norm).toPrecision(7)));
}

// ---------- run ----------

export type RunOptions = { mode: string; budgetSeconds: number; batchSize: number };

export async function run(db: Db, apiKey: string, options: RunOptions) {
  const started = Date.now();
  const deadline = started + options.budgetSeconds * 1000;
  const batchSize = Math.min(Math.max(options.batchSize, 1), MAX_BATCH);

  const runId = await rpc<number | null>(db, "kb_embed_begin", { p_mode: options.mode });
  if (runId === null) return { status: "busy", note: "another kb-embed run is active" };

  const result = { status: "ok", embedded: 0, memory_embedded: 0, batches: 0, errors: [] as string[] };
  const skipped: string[] = [];
  let legacy = false;

  try {
    sources: for (const source of SOURCES) {
      // Leave room for one more Gemini call + store before the Edge wall clock (150 s).
      while (Date.now() < deadline - 15_000) {
        let rows: Row[];
        try {
          rows = await rpc<Row[]>(db, source.queue, { p_limit: batchSize }, `?select=${source.select}`);
        } catch (error) {
          if (error instanceof MissingRpc) {
            skipped.push(source.name);
            continue sources;
          }
          throw error;
        }
        if (!rows.length) break;

        const items = rows.map((row) => ({
          text: (source.text(row).trim() || source.title(row) || "(empty)").slice(0, MAX_CHARS),
          title: source.title(row)?.slice(0, 300),
        }));

        let response = await callGemini(apiKey, items, legacy);
        if (response.status === 400 && !legacy) {
          const text = await response.text();
          if (/embedContentConfig|Unknown name|Invalid JSON payload/i.test(text)) {
            legacy = true;
            response = await callGemini(apiKey, items, legacy);
          } else {
            result.status = "error";
            result.errors.push(`gemini 400: ${text.slice(0, 300)}`);
            break sources;
          }
        }
        if (response.status === 429) {
          // Quota or rate limit: stop and leave the rest for the next run.
          result.status = "rate_limited";
          result.errors.push(`gemini 429: ${(await response.text()).slice(0, 300)}`);
          break sources;
        }
        if (!response.ok) {
          result.status = "error";
          result.errors.push(`gemini ${response.status}: ${(await response.text()).slice(0, 300)}`);
          break sources;
        }

        const data = await response.json() as { embeddings?: { values: number[] }[] };
        const embeddings = data.embeddings ?? [];
        if (embeddings.length !== rows.length) {
          result.status = "error";
          result.errors.push(`gemini returned ${embeddings.length} embeddings for ${rows.length} inputs`);
          break sources;
        }

        const payload = rows.map((row, i) => ({
          id: row.id,
          content_hash: row.content_hash,
          model: MODEL_TAG,
          embedding: normalise(embeddings[i].values),
        }));
        const stored = await rpc<number>(db, source.store, { p_rows: payload });
        result[source.counter] += stored;
        result.batches += 1;

        if (stored === 0) break; // every row changed while embedding; they come back with the new hash next run
        if (rows.length < batchSize) break; // queue drained
      }
    }
  } catch (error) {
    result.status = "error";
    result.errors.push(String(error).slice(0, 300));
  }

  const details = { seconds: Math.round((Date.now() - started) / 100) / 10, legacy_request_shape: legacy, skipped };
  const pending = await rpc<object>(db, "kb_embed_finish", { p_id: runId, p_result: { ...result, details } });
  return { run_id: runId, ...result, ...details, pending };
}
