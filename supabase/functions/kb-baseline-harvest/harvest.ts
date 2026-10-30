// Baseline harvest (WS8, docs/agent-context-rag-implementation.md §5 WS8 8.3): a one-off TED harvest into
// kb_baseline_notices for one kb_snapshots row (status 'building'). Resumable: the cursor lives in
// kb_snapshots.harvest_state and is saved after every page. Polite: at most one TED request per second,
// at most 40 fields and 10,000 fields per page, paginationMode ITERATION. TED counts one implicit field per
// notice on top of the requested ones, so the 40-field main pass uses 240 notices per page
// (41 x 240 = 9,840 fields) and the 14-field extra pass 250.
//
// Phases, in order:
//   1. main   every scope query of kb_snapshots.queries, 40 fields (the 38 of ted-sync + procedure-identifier,
//             notice-identifier)
//   2. extra  the same queries again with 14 small fields: result status (BT-142/BT-144), previous notice
//             references, lots and quantities. Kept separate so a page never exceeds 40 fields.
//   3. link   the tenders of harvested results that are not in the snapshot yet: by procedure-identifier,
//             then by the publication numbers in previous-notice-id-proc (both passes per batch).
//
// The normalisation helpers are copied from ../ted-sync/sync.ts, so baseline rows match the live layer.

export const TED_API = "https://api.ted.europa.eu/v3/notices/search";
const PAGE_SIZE_MAIN = 240; // (40 + 1) x 240 = 9,840 fields per page; 250 would be 10,250 (TED rejects > 10,000)
const PAGE_SIZE_EXTRA = 250; // (14 + 1) x 250 = 3,750
const MIN_INTERVAL_MS = 1100; // ≤ 1 request per second
const LINK_BATCH = 100; // ids per linking query (~3.8 kB query, one page of results)

// 38 fields of ted-sync + 2 identifiers = 40 fields.
export const FIELDS_MAIN = [
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
  "estimated-value-lot",
  "estimated-value-cur-lot",
  "framework-maximum-value-lot",
  "framework-maximum-value-cur-lot",
  "deadline-receipt-tender-date-lot",
  "DT",
  "deadline-receipt-request",
  "winner-name",
  "winner-identifier",
  "winner-country",
  "winner-size",
  "winner-owner-nationality",
  "winner-decision-date",
  "result-value-notice",
  "result-value-cur-notice",
  "total-value",
  "total-value-cur",
  "received-submissions-type-val",
  "subcontracting",
  "direct-award-justification-proc",
  "place-of-performance",
  "procedure-identifier",
  "notice-identifier",
];

// 14 small fields.
export const FIELDS_EXTRA = [
  "publication-number",
  "notice-version",
  "winner-selection-status", // BT-142: selec-w (winner chosen), clos-nw (closed, no winner), open-nw
  "non-award-justification", // BT-144: no-rece, all-rej, chan-need, ins-fund, tch-pr-error, other ...
  "previous-notice-id-proc", // the tender's publication number (or notice id) referenced by a result
  "modification-previous-notice-identifier",
  "tendering-party-name", // consortia: one tendering party, several winners
  "identifier-lot",
  "result-lot-identifier",
  "quantity-lot", // BT-25
  "quantity-unit-lot", // BT-625 (H87 = piece)
  "tender-value",
  "tender-value-cur",
  "contract-conclusion-date",
];

export const COMPETITION_TYPES =
  "notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social)";

export type Kind = "competition" | "result" | "modification";
type Notice = Record<string, unknown>;
export type Db = { url: string; key: string };
type ScopeQuery = { key: string; kind: Kind; query: string };
type Cursor = { token: string | null; done: boolean; fetched: number; stored: number; total: number | null; pages: number };
type LinkCursor = { after: string | null; done: boolean; batches: number; requested: number; fetched: number };
type Pass = "main" | "extra";
export type HarvestState = {
  version: 1;
  main: Record<string, Cursor>;
  extra: Record<string, Cursor>;
  link: { procedure: LinkCursor; previous: LinkCursor };
  done: boolean;
  runs: number;
  requests: number;
  started_at: string;
  updated_at: string;
  finished_at?: string;
};

// ---------- value helpers (copied from ted-sync: strings, lists repeated per lot, or {lang: value}) ----------

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

// Latest calendar date as written in the notice (offset dropped, like publication_date).
function latestDate(value: unknown): string | null {
  const dates = list(value).map((v) => v.slice(0, 10)).filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d)).sort();
  return dates.at(-1) ?? null;
}

function withoutLinks(n: Notice): Notice {
  const { links: _links, ...rest } = n;
  return rest;
}

// ---------- normalisation into kb_baseline_notices rows ----------

// Winner lists are flattened across lots; pair them by position, then dedupe (as ted-sync awardRows).
export function winners(n: Notice) {
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
      name: name.trim(),
      identifier: ids[i] ?? null,
      country: countries[i] ?? null,
      size: sizes[i] ?? null,
      owner_nationality: owners[i] ?? null,
    });
  });
  return [...byKey.values()];
}

export function noticeRow(n: Notice) {
  const cpv = uniq(list(n["classification-cpv"]));
  const title = byLanguage(n["title-proc"]);
  const description = byLanguage(n["description-proc"]);
  const values = [
    { source: "procedure", value: num(n["estimated-value-proc"]), currency: first(n["estimated-value-cur-proc"]) },
    { source: "lots", value: sum(n["estimated-value-lot"]), currency: first(n["estimated-value-cur-lot"]) },
    { source: "framework_max", value: sum(n["framework-maximum-value-lot"]), currency: first(n["framework-maximum-value-cur-lot"]) },
  ];
  const estimate = values.find((v) => v.value !== null);
  const deadlineTender =
    latest(n["deadline-receipt-tender-date-lot"]) ?? (n["deadline-receipt-request"] ? null : latest(n["DT"])); // pre-eForms: "tenders or requests"
  const deadlineRequest = latest(n["deadline-receipt-request"]);
  return {
    publication_number: String(n["publication-number"]),
    procedure_id: first(n["procedure-identifier"]),
    notice_identifier: first(n["notice-identifier"]),
    notice_type: first(n["notice-type"]),
    publication_date: first(n["publication-date"])?.slice(0, 10) ?? null,
    legal_basis: first(n["legal-basis"]),
    procedure_type: first(n["procedure-type"]),
    contract_nature: uniq(list(n["contract-nature"])),
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
    estimated_value: estimate?.value ?? null,
    estimated_currency: estimate?.currency ?? null,
    estimated_value_source: estimate?.source ?? null,
    deadline: deadlineTender ?? deadlineRequest,
    deadline_kind: deadlineTender ? "tenders" : deadlineRequest ? "requests_to_participate" : null,
    result_value: num(n["result-value-notice"]) ?? num(n["total-value"]),
    result_currency: first(n["result-value-cur-notice"]) ?? first(n["total-value-cur"]),
    bids_received: num(n["received-submissions-type-val"]),
    winners: winners(n),
    decision_date: latestDate(n["winner-decision-date"]),
    place_of_performance: uniq(list(n["place-of-performance"])),
    direct_award_justification: first(n["direct-award-justification-proc"]),
    raw: withoutLinks(n),
  };
}

// Quantity only when the notice has a single lot with exactly one stated quantity: on multi-lot notices
// the flat JSON lists cannot be paired with values (lot-level truth needs the eForms XML).
export function extraRow(n: Notice) {
  const lots = uniq(list(n["identifier-lot"]));
  const resultLots = uniq(list(n["result-lot-identifier"]));
  const quantities = list(n["quantity-lot"]).map((q) => num(q)).filter((q): q is number => q !== null);
  const units = uniq(list(n["quantity-unit-lot"]));
  const lotCount = Math.max(lots.length, resultLots.length);
  const single = lotCount <= 1 && quantities.length === 1 && quantities[0] > 0;
  const version = num(n["notice-version"]);
  return {
    publication_number: String(n["publication-number"]),
    notice_version: version === null ? null : Math.trunc(version),
    winner_selection_status: uniq(list(n["winner-selection-status"])),
    non_award_justification: uniq(list(n["non-award-justification"])),
    previous_notice_ids: uniq(list(n["previous-notice-id-proc"])),
    modified_notice_ids: uniq(list(n["modification-previous-notice-identifier"])),
    tendering_parties: uniq(list(n["tendering-party-name"]).map((s) => s.trim())),
    n_lots: lotCount || null,
    quantity: single ? quantities[0] : null,
    quantity_unit: single ? (units[0] ?? null) : null,
    contract_conclusion_date: latestDate(n["contract-conclusion-date"]),
    raw: withoutLinks(n),
  };
}

// ---------- TED API (polite) ----------

let lastRequest = 0;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function throttle() {
  const wait = lastRequest + MIN_INTERVAL_MS - Date.now();
  if (wait > 0) await sleep(wait);
  lastRequest = Date.now();
}

export async function fetchPage(query: string, fields: string[], token?: string | null) {
  const body: Record<string, unknown> = {
    query,
    fields,
    limit: fields.length > 20 ? PAGE_SIZE_MAIN : PAGE_SIZE_EXTRA,
    scope: "ALL",
    paginationMode: "ITERATION",
  };
  if (token) body.iterationNextToken = token;
  for (let attempt = 1; ; attempt++) {
    await throttle();
    const response = await fetch(TED_API, {
      method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": "salespatriot-kb-baseline/1.0" },
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
    await sleep(3000 * attempt);
  }
}

// ---------- Supabase REST (PostgREST) ----------

function headers(db: Db, extra: Record<string, string> = {}): Record<string, string> {
  // Legacy service_role keys are JWTs and go in Authorization too; sb_secret_ keys only in apikey.
  const auth: Record<string, string> = db.key.startsWith("eyJ") ? { Authorization: `Bearer ${db.key}` } : {};
  return { apikey: db.key, ...auth, "Content-Type": "application/json", ...extra };
}

async function rpc<T>(db: Db, name: string, args: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${db.url}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: headers(db),
    body: JSON.stringify(args),
  });
  if (!response.ok) throw new Error(`rpc ${name} ${response.status}: ${(await response.text()).slice(0, 300)}`);
  return response.json();
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

// The secret lives only in Vault; public.kb_baseline_check_secret is callable by service_role only.
export async function checkSecret(db: Db, candidate: string | null): Promise<boolean> {
  if (!candidate) return false;
  try {
    return (await rpc<boolean>(db, "kb_baseline_check_secret", { candidate })) === true;
  } catch {
    return false;
  }
}

async function saveState(db: Db, snapshot: string, state: HarvestState) {
  state.updated_at = new Date().toISOString();
  const response = await fetch(`${db.url}/rest/v1/kb_snapshots?id=eq.${encodeURIComponent(snapshot)}`, {
    method: "PATCH",
    headers: headers(db, { Prefer: "return=minimal" }),
    body: JSON.stringify({ harvest_state: state }),
  });
  if (!response.ok) throw new Error(`save state ${response.status}: ${(await response.text()).slice(0, 300)}`);
}

async function store(db: Db, snapshot: string, key: string, kind: Kind, pass: Pass, notices: Notice[]) {
  if (!notices.length) return 0;
  if (pass === "main") {
    return await rpc<number>(db, "kb_baseline_ingest", {
      p_snapshot: snapshot,
      p_query: key,
      p_kind: kind,
      p_rows: notices.map(noticeRow),
    });
  }
  return await rpc<number>(db, "kb_baseline_ingest_extra", { p_snapshot: snapshot, p_rows: notices.map(extraRow) });
}

// ---------- run ----------

const cursor = (): Cursor => ({ token: null, done: false, fetched: 0, stored: 0, total: null, pages: 0 });
const linkCursor = (): LinkCursor => ({ after: null, done: false, batches: 0, requested: 0, fetched: 0 });

function freshState(): HarvestState {
  const now = new Date().toISOString();
  return {
    version: 1,
    main: {},
    extra: {},
    link: { procedure: linkCursor(), previous: linkCursor() },
    done: false,
    runs: 0,
    requests: 0,
    started_at: now,
    updated_at: now,
  };
}

type Snapshot = { id: string; status: string; cutoff_date: string; queries: ScopeQuery[]; harvest_state: HarvestState | Record<string, never> };

export async function runHarvest(db: Db, snapshot: string, budgetSeconds = 100, restart = false) {
  const deadline = Date.now() + budgetSeconds * 1000;
  const rows = await select<Snapshot[]>(
    db,
    `kb_snapshots?id=eq.${encodeURIComponent(snapshot)}&select=id,status,cutoff_date,queries,harvest_state`,
  );
  const snap = rows[0];
  if (!snap) throw new Error(`unknown snapshot ${snapshot}`);
  if (snap.status !== "building") throw new Error(`snapshot ${snapshot} is ${snap.status}; harvesting is closed`);

  const cutoff = snap.cutoff_date.replaceAll("-", "");
  const scope = (snap.queries ?? []).filter((q) => !q.key.startsWith("link_"));
  const state: HarvestState =
    !restart && (snap.harvest_state as HarvestState)?.version === 1 ? (snap.harvest_state as HarvestState) : freshState();
  state.runs += 1;
  const run = { requests: 0, stored: 0 };

  const page = async (query: string, fields: string[], token: string | null) => {
    run.requests += 1;
    state.requests += 1;
    return await fetchPage(query, fields, token);
  };

  // 1 + 2: scope queries, main pass then extra pass
  for (const pass of ["main", "extra"] as Pass[]) {
    for (const q of scope) {
      const cur = (state[pass][q.key] ??= cursor());
      while (!cur.done && Date.now() < deadline) {
        const result = await page(q.query, pass === "main" ? FIELDS_MAIN : FIELDS_EXTRA, cur.token);
        const stored = await store(db, snapshot, q.key, q.kind, pass, result.notices);
        cur.fetched += result.notices.length;
        cur.stored += stored;
        run.stored += stored;
        cur.total ??= result.total;
        cur.pages += 1;
        cur.token = result.next;
        // TED returns a next token even on the last page: stop once the total is reached
        cur.done = !result.notices.length || !result.next || (cur.total !== null && cur.fetched >= cur.total);
        await saveState(db, snapshot, state);
      }
      if (!cur.done) return finish(db, snapshot, state, run);
    }
  }

  // 3: tenders of results that are not in the snapshot yet
  for (const mode of ["procedure", "previous"] as const) {
    const cur = state.link[mode];
    const key = `link_${mode}`;
    const field = mode === "procedure" ? "procedure-identifier" : "publication-number";
    while (!cur.done && Date.now() < deadline) {
      const ids = await rpc<string[]>(db, "kb_baseline_link_targets", {
        p_snapshot: snapshot,
        p_mode: mode,
        p_after: cur.after,
        p_limit: LINK_BATCH,
      });
      if (!ids?.length) {
        cur.done = true;
        await saveState(db, snapshot, state);
        break;
      }
      const query = `${field} IN (${ids.join(" ")}) AND ${COMPETITION_TYPES} AND PD<=${cutoff}`;
      for (const pass of ["main", "extra"] as Pass[]) {
        let token: string | null = null;
        let got = 0;
        do {
          const result = await page(query, pass === "main" ? FIELDS_MAIN : FIELDS_EXTRA, token);
          const stored = await store(db, snapshot, key, "competition", pass, result.notices);
          got += result.notices.length;
          if (pass === "main") {
            cur.fetched += result.notices.length;
            run.stored += stored;
          }
          token = result.notices.length && (result.total === null || got < result.total) ? result.next : null;
        } while (token);
      }
      cur.after = ids[ids.length - 1];
      cur.batches += 1;
      cur.requested += ids.length;
      await saveState(db, snapshot, state);
    }
    if (!cur.done) return finish(db, snapshot, state, run);
  }

  state.done = true;
  state.finished_at ??= new Date().toISOString();
  return finish(db, snapshot, state, run);
}

async function finish(db: Db, snapshot: string, state: HarvestState, run: { requests: number; stored: number }) {
  await saveState(db, snapshot, state);
  const summary = (cursors: Record<string, Cursor>) =>
    Object.fromEntries(Object.entries(cursors).map(([k, c]) => [k, { fetched: c.fetched, total: c.total, done: c.done }]));
  return {
    snapshot,
    done: state.done,
    run,
    main: summary(state.main),
    extra: summary(state.extra),
    link: state.link,
    runs: state.runs,
    requests: state.requests,
  };
}

export async function status(db: Db, snapshot: string) {
  const base = `kb_baseline_notices?snapshot_id=eq.${encodeURIComponent(snapshot)}&select=publication_number`;
  const [snap, competition, result, modification] = await Promise.all([
    select<Record<string, unknown>[]>(
      db,
      `kb_snapshots?id=eq.${encodeURIComponent(snapshot)}&select=id,status,active,cutoff_date,harvest_state,counts,frozen_at`,
    ),
    count(db, `${base}&kind=eq.competition`),
    count(db, `${base}&kind=eq.result`),
    count(db, `${base}&kind=eq.modification`),
  ]);
  return { snapshot: snap[0] ?? null, notices: { competition, result, modification } };
}
