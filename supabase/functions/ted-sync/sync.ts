// TED → Supabase sync for our four focus CPV classes (knowledge_base/21-focus-scope.md).
// Plain TypeScript + fetch, so it runs in Deno (Edge Function) and in Node (local tests).

export const TED_API = "https://api.ted.europa.eu/v3/notices/search";
const PAGE_SIZE = 250;
// 38 fields × 250 notices = 9,500 fields per page, under TED's 10,000 limit.
const FIELDS = [
  "publication-number",
  "notice-type",
  "publication-date",
  "legal-basis",
  "procedure-type",
  "contract-nature",
  "classification-cpv",
  "notice-title",
  "title-proc",
  "description-proc",
  "buyer-name",
  "buyer-identifier",
  "buyer-country",
  "buyer-legal-type",
  "main-activity",
  "estimated-value-proc",
  "estimated-value-cur-proc",
  // Some buyers give the value only per lot or as a framework maximum.
  "estimated-value-lot",
  "estimated-value-cur-lot",
  "framework-maximum-value-lot",
  "framework-maximum-value-cur-lot",
  "deadline-receipt-tender-date-lot",
  "DT", // generic deadline; used only for pre-eForms notices (for eForms it repeats the request deadline)
  "deadline-receipt-request",
  "winner-name",
  "winner-identifier",
  "winner-country",
  "winner-size",
  "winner-owner-nationality",
  "winner-decision-date",
  "result-value-notice",
  "result-value-cur-notice",
  // Pre-eForms notices (before late 2023) only carry the total value, no winner fields.
  "total-value",
  "total-value-cur",
  "received-submissions-type-val",
  "subcontracting",
  "direct-award-justification-proc",
  "place-of-performance",
];
const FOCUS_CLASSES: Record<string, string> = {
  "3531": "miscellaneous_weapons",
  "3532": "firearms",
  "3533": "ammunition",
  "3534": "parts",
};
// "Gunmetal pipe fittings" sits under parts of firearms but is plumbing.
const NOISE_CPV = new Set(["35341100"]);
const SCOPE =
  "contract-nature IN (supplies) AND classification-cpv IN (35310000 35320000 35330000 35340000)";
export const QUERIES = {
  competition:
    "notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social) AND " +
    SCOPE,
  result: "notice-type IN (can-standard can-desg can-social can-tran) AND " + SCOPE,
} as const;
export type Kind = keyof typeof QUERIES;

type Notice = Record<string, unknown>;
export type Db = { url: string; key: string };

// ---------- value helpers (TED returns strings, lists repeated per lot, or {lang: value}) ----------

function list(value: unknown): string[] {
  if (value == null || value === "") return [];
  if (Array.isArray(value)) return value.flatMap(list);
  if (typeof value === "object") return Object.values(value as object).flatMap(list);
  return [String(value)];
}

const uniq = (values: string[]) => [...new Set(values.filter(Boolean))];
const first = (value: unknown) => list(value)[0] ?? null;

function byLanguage(value: unknown): { text: string | null; lang: string | null } {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    const [lang, text] = entries.find(([l]) => l === "eng") ?? entries[0] ?? [null, null];
    return { text: first(text), lang };
  }
  return { text: first(value), lang: null };
}

function num(value: unknown): number | null {
  const text = first(value);
  if (text == null) return null;
  const parsed = Number(text.replace(/\s/g, "").replace(",", "."));
  return Number.isFinite(parsed) ? parsed : null;
}

// Lot fields hold one entry per lot, so equal values are separate lots and must all count.
function sum(value: unknown): number | null {
  const values = list(value).map((text) => num(text)).filter((v): v is number => v !== null);
  return values.length ? values.reduce((a, b) => a + b, 0) : null;
}

// "2026-11-27+02:00" (date only) counts as open until the end of that day.
function timestamp(value: string): string | null {
  const dateOnly = value.match(/^(\d{4}-\d{2}-\d{2})(Z|[+-]\d{2}:\d{2})?$/);
  const text = dateOnly ? `${dateOnly[1]}T23:59:59${dateOnly[2] ?? "Z"}` : value;
  const time = Date.parse(text);
  return Number.isNaN(time) ? null : new Date(time).toISOString();
}

// Latest lot deadline, so a notice stays open while any lot is open.
function latest(value: unknown): string | null {
  const times = list(value).map(timestamp).filter((t): t is string => t !== null).sort();
  return times.at(-1) ?? null;
}

function focusClasses(cpv: string[]): string[] {
  const relevant = cpv.filter((code) => !NOISE_CPV.has(code));
  return uniq(
    relevant.flatMap((code) => (FOCUS_CLASSES[code.slice(0, 4)] ? [FOCUS_CLASSES[code.slice(0, 4)]] : [])),
  );
}

// ---------- normalisation into table rows ----------

export function noticeRow(n: Notice, kind: Kind, now: string) {
  const cpv = uniq(list(n["classification-cpv"]));
  const title = byLanguage(n["title-proc"]);
  const description = byLanguage(n["description-proc"]);
  const values = [
    { source: "procedure", value: num(n["estimated-value-proc"]), currency: first(n["estimated-value-cur-proc"]) },
    { source: "lots", value: sum(n["estimated-value-lot"]), currency: first(n["estimated-value-cur-lot"]) },
    { source: "framework_max", value: sum(n["framework-maximum-value-lot"]), currency: first(n["framework-maximum-value-cur-lot"]) },
  ];
  const estimate = values.find((v) => v.value !== null);
  return {
    publication_number: String(n["publication-number"]),
    kind,
    notice_type: first(n["notice-type"]),
    publication_date: first(n["publication-date"])?.slice(0, 10) ?? null,
    legal_basis: first(n["legal-basis"]),
    procedure_type: first(n["procedure-type"]),
    title_en: (n["notice-title"] as Record<string, string> | undefined)?.eng ?? byLanguage(n["notice-title"]).text,
    title_original: title.text,
    description: description.text,
    original_language: description.lang ?? title.lang,
    buyer_name: byLanguage(n["buyer-name"]).text,
    buyer_identifier: first(n["buyer-identifier"]),
    buyer_country: first(n["buyer-country"]),
    buyer_legal_type: first(n["buyer-legal-type"]),
    main_activity: first(n["main-activity"]),
    cpv_codes: cpv,
    focus_classes: focusClasses(cpv),
    estimated_value: estimate?.value ?? null,
    estimated_currency: estimate?.currency ?? null,
    estimated_value_source: estimate?.source ?? null,
    deadline_tender:
      latest(n["deadline-receipt-tender-date-lot"]) ??
      (n["deadline-receipt-request"] ? null : latest(n["DT"])), // pre-eForms: "tenders or requests"
    deadline_request: latest(n["deadline-receipt-request"]),
    result_value: num(n["result-value-notice"]) ?? num(n["total-value"]),
    result_currency: first(n["result-value-cur-notice"]) ?? first(n["total-value-cur"]),
    bids_received: num(n["received-submissions-type-val"]),
    subcontracting: first(n["subcontracting"]),
    direct_award_justification: first(n["direct-award-justification-proc"]),
    place_of_performance: uniq(list(n["place-of-performance"])),
    raw: n,
    updated_at: now,
  };
}

// Winner lists are flattened across lots; pair them by position, then dedupe.
// Lot-level winner ↔ value pairing needs the eForms XML (ted_supplier_contracts.py).
export function awardRows(n: Notice, now: string) {
  const names = list(n["winner-name"]);
  const ids = list(n["winner-identifier"]);
  const countries = list(n["winner-country"]);
  const sizes = list(n["winner-size"]);
  const owners = list(n["winner-owner-nationality"]);
  const byKey = new Map<string, Record<string, unknown>>();
  names.forEach((name, i) => {
    const key = name.trim().toLowerCase();
    if (!key || byKey.has(key)) return;
    byKey.set(key, {
      publication_number: String(n["publication-number"]),
      winner_key: key,
      winner_name: name.trim(),
      winner_identifier: ids[i] ?? null,
      winner_country: countries[i] ?? null,
      winner_size: sizes[i] ?? null,
      winner_owner_nationality: owners[i] ?? null,
      decision_date: first(n["winner-decision-date"])?.slice(0, 10) ?? null,
      notice_value: num(n["result-value-notice"]),
      currency: first(n["result-value-cur-notice"]),
      updated_at: now,
    });
  });
  const rows = [...byKey.values()];
  return rows.map((row) => ({ ...row, co_winners: rows.length }));
}

// ---------- TED API ----------

export async function fetchPage(query: string, token?: string) {
  const body: Record<string, unknown> = {
    query,
    fields: FIELDS,
    limit: PAGE_SIZE,
    scope: "ALL",
    paginationMode: "ITERATION",
  };
  if (token) body.iterationNextToken = token;
  for (let attempt = 1; ; attempt++) {
    const response = await fetch(TED_API, {
      method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": "salespatriot-ted-sync/1.0" },
      body: JSON.stringify(body),
    });
    if (response.ok) {
      const data = await response.json();
      return {
        notices: (data.notices ?? []) as Notice[],
        next: (data.iterationNextToken as string | undefined) ?? null,
        total: (data.totalNoticeCount as number | undefined) ?? null,
      };
    }
    if (attempt >= 4 || (response.status < 500 && response.status !== 429)) {
      throw new Error(`TED API ${response.status}: ${(await response.text()).slice(0, 300)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
  }
}

// ---------- Supabase REST (PostgREST) ----------

function headers(db: Db, extra: Record<string, string> = {}): Record<string, string> {
  // Legacy service_role keys are JWTs and go in Authorization too; sb_secret_ keys only in apikey.
  const auth: Record<string, string> = db.key.startsWith("eyJ") ? { Authorization: `Bearer ${db.key}` } : {};
  return { apikey: db.key, ...auth, "Content-Type": "application/json", ...extra };
}

// The cron secret lives only in Vault; public.ted_sync_check_secret is callable by service_role only.
export async function checkSecret(db: Db, candidate: string | null): Promise<boolean> {
  if (!candidate) return false;
  const response = await fetch(`${db.url}/rest/v1/rpc/ted_sync_check_secret`, {
    method: "POST",
    headers: headers(db),
    body: JSON.stringify({ candidate }),
  });
  return response.ok && (await response.json()) === true;
}

async function upsert(db: Db, table: string, onConflict: string, rows: unknown[]) {
  for (let i = 0; i < rows.length; i += 500) {
    const response = await fetch(`${db.url}/rest/v1/${table}?on_conflict=${onConflict}`, {
      method: "POST",
      headers: headers(db, { Prefer: "resolution=merge-duplicates,return=minimal" }),
      body: JSON.stringify(rows.slice(i, i + 500)),
    });
    if (!response.ok) throw new Error(`upsert ${table} ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
}

async function select<T>(db: Db, path: string): Promise<T> {
  const response = await fetch(`${db.url}/rest/v1/${path}`, { headers: headers(db) });
  if (!response.ok) throw new Error(`select ${path} ${response.status}: ${(await response.text()).slice(0, 300)}`);
  return response.json();
}

async function count(db: Db, path: string): Promise<number | null> {
  const response = await fetch(`${db.url}/rest/v1/${path}`, {
    method: "HEAD",
    headers: headers(db, { Prefer: "count=exact" }),
  });
  const range = response.headers.get("content-range");
  return range ? Number(range.split("/")[1]) : null;
}

async function store(db: Db, notices: Notice[], kind: Kind) {
  const now = new Date().toISOString();
  const noticeRows = notices.map((n) => noticeRow(n, kind, now));
  const awards = kind === "result" ? notices.flatMap((n) => awardRows(n, now)) : [];
  await upsert(db, "ted_notices", "publication_number", noticeRows);
  if (awards.length) await upsert(db, "ted_awards", "publication_number,winner_key", awards);
  return { notices: noticeRows.length, awards: awards.length };
}

async function logRun(db: Db, run: Record<string, unknown>) {
  await fetch(`${db.url}/rest/v1/ted_sync_runs`, {
    method: "POST",
    headers: headers(db, { Prefer: "return=minimal" }),
    body: JSON.stringify(run),
  });
}

// ---------- run modes ----------

// New and updated notices from the last `days` days (2 by default: TED publishes once per working day).
export async function runIncremental(db: Db, days = 2) {
  const started = new Date().toISOString();
  const totals = { fetched: 0, notices: 0, awards: 0 };
  try {
    for (const kind of Object.keys(QUERIES) as Kind[]) {
      let token: string | undefined;
      do {
        const page = await fetchPage(`${QUERIES[kind]} AND PD>=today(-${days})`, token);
        if (!page.notices.length) break;
        const saved = await store(db, page.notices, kind);
        totals.fetched += page.notices.length;
        totals.notices += saved.notices;
        totals.awards += saved.awards;
        token = page.next ?? undefined;
      } while (token);
    }
    await logRun(db, { mode: "incremental", started_at: started, fetched: totals.fetched, notices_upserted: totals.notices, awards_upserted: totals.awards, done: true, details: { days } });
    return { mode: "incremental", days, ...totals };
  } catch (error) {
    await logRun(db, { mode: "incremental", started_at: started, ...{ fetched: totals.fetched }, done: false, error: String(error) });
    throw error;
  }
}

type BackfillState = Record<Kind, { token: string | null; done: boolean; fetched: number }>;

// Full history, resumable: works through pages until the time budget runs out, saves the
// iteration cursor (valid until the next OJ S release + 24 h) and returns `done`.
export async function runBackfill(db: Db, budgetSeconds = 100, restart = false) {
  const started = new Date().toISOString();
  const deadline = Date.now() + budgetSeconds * 1000;
  const saved = restart ? [] : await select<{ value: BackfillState }[]>(db, "ted_sync_state?key=eq.backfill&select=value");
  const state: BackfillState = saved[0]?.value ?? {
    competition: { token: null, done: false, fetched: 0 },
    result: { token: null, done: false, fetched: 0 },
  };
  const totals = { fetched: 0, notices: 0, awards: 0 };
  for (const kind of Object.keys(QUERIES) as Kind[]) {
    while (!state[kind].done && Date.now() < deadline) {
      const page = await fetchPage(QUERIES[kind], state[kind].token ?? undefined);
      if (page.notices.length) {
        const result = await store(db, page.notices, kind);
        totals.fetched += page.notices.length;
        totals.notices += result.notices;
        totals.awards += result.awards;
        state[kind].fetched += page.notices.length;
      }
      state[kind].token = page.next;
      state[kind].done = !page.notices.length || !page.next;
    }
  }
  const done = state.competition.done && state.result.done;
  await upsert(db, "ted_sync_state", "key", [{ key: "backfill", value: state, updated_at: new Date().toISOString() }]);
  await logRun(db, { mode: "backfill", started_at: started, fetched: totals.fetched, notices_upserted: totals.notices, awards_upserted: totals.awards, done, details: state });
  return { mode: "backfill", done, ...totals, progress: state };
}

export async function status(db: Db) {
  const [notices, awards, open, runs] = await Promise.all([
    count(db, "ted_notices?select=publication_number"),
    count(db, "ted_awards?select=publication_number"),
    count(db, "ted_open_tenders?select=publication_number"),
    select(db, "ted_sync_runs?select=mode,started_at,finished_at,fetched,notices_upserted,awards_upserted,done,error&order=id.desc&limit=5"),
  ]);
  return { notices, awards, open_tenders: open, last_runs: runs };
}
