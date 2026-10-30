// Lot-level data for a baseline snapshot (baseline_v2): fetch TED result-notice XML, parse lots, upsert
// kb_baseline_lots. Server-side twin of ted_baseline_lots.py (same rules, same output columns); the raw XML is
// parsed in memory and never stored. The list of notices is kb_snapshots.harvest_state.lots.sample; progress is
// kept in harvest_state.lots, so a run resumes where the last one stopped.

export type Db = { url: string; key: string };
type Row = Record<string, unknown>;

const XML_URL = (n: string) => `https://ted.europa.eu/en/notice/${n}/xml`;
const MIN_INTERVAL_MS = 1000;
const MAX_RETRIES = 4;
const TITLE_CHARS = 300;
const DESCRIPTION_CHARS = 300;

// ISO 3166 alpha-2 (and the TED legacy codes UK, EL) -> alpha-3 (eForms / Search API)
const ALPHA3: Record<string, string> = {
  AT: "AUT", BE: "BEL", BG: "BGR", HR: "HRV", CY: "CYP", CZ: "CZE", DK: "DNK", EE: "EST", FI: "FIN", FR: "FRA",
  DE: "DEU", GR: "GRC", EL: "GRC", HU: "HUN", IE: "IRL", IT: "ITA", LV: "LVA", LT: "LTU", LU: "LUX", MT: "MLT",
  NL: "NLD", PL: "POL", PT: "PRT", RO: "ROU", SK: "SVK", SI: "SVN", ES: "ESP", SE: "SWE", GB: "GBR", UK: "GBR",
  NO: "NOR", IS: "ISL", LI: "LIE", CH: "CHE", US: "USA", CA: "CAN", IL: "ISR", TR: "TUR", UA: "UKR", RS: "SRB",
  BA: "BIH", ME: "MNE", MK: "MKD", AL: "ALB", MD: "MDA", KR: "KOR", JP: "JPN", AU: "AUS", ZA: "ZAF", BR: "BRA",
  AE: "ARE", SG: "SGP", IN: "IND", CN: "CHN", XK: "XKX", GE: "GEO", AR: "ARG", MX: "MEX", NZ: "NZL", SA: "SAU",
  TW: "TWN", HK: "HKG", BY: "BLR", RU: "RUS",
};

export function redact(text: string): string {
  return text
    .replace(/(Bearer|apikey:?)\s+\S+/gi, "$1 [redacted]")
    .replace(/eyJ[\w-]+\.[\w-]+\.[\w-]+/g, "[redacted jwt]")
    .replace(/sb_(secret|publishable)_[\w-]+/g, "[redacted key]")
    .replace(/AIza[\w-]{20,}/g, "[redacted key]")
    .slice(0, 1000);
}

// ------------------------------------------------------------------ minimal XML tree (namespaces stripped)

type El = { tag: string; attrs: Record<string, string>; children: El[]; text: string; parent: El | null };

const localName = (name: string) => name.slice(name.lastIndexOf(":") + 1);

function decode(s: string): string {
  return s.replace(/&(#x[0-9a-fA-F]+|#\d+|lt|gt|amp|quot|apos);/g, (_, e: string) => {
    if (e[0] === "#") return String.fromCodePoint(e[1] === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return ({ lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" } as Record<string, string>)[e];
  });
}

export function parseXml(src: string): El | null {
  const root: El = { tag: "#root", attrs: {}, children: [], text: "", parent: null };
  let top = root;
  const re =
    /<!\[CDATA\[([\s\S]*?)\]\]>|<!--[\s\S]*?-->|<\?[\s\S]*?\?>|<!DOCTYPE[^>]*>|<\/([^\s>]+)\s*>|<([^\s>\/!?]+)((?:\s+[^\s=>\/]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|([^<]+)/g;
  const attrRe = /([^\s=]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src))) {
    if (m[1] !== undefined) top.text += m[1];
    else if (m[2] !== undefined) top = top.parent ?? root;
    else if (m[3] !== undefined) {
      const attrs: Record<string, string> = {};
      let a: RegExpExecArray | null;
      attrRe.lastIndex = 0;
      while ((a = attrRe.exec(m[4] ?? ""))) {
        if (!a[1].startsWith("xmlns")) attrs[localName(a[1])] = decode(a[2] ?? a[3] ?? "");
      }
      const el: El = { tag: localName(m[3]), attrs, children: [], text: "", parent: top };
      top.children.push(el);
      if (!m[5]) top = el;
    } else if (m[6] !== undefined) top.text += decode(m[6]);
  }
  return root.children[0] ?? null;
}

function* iter(el: El | null | undefined, tag?: string): Generator<El> {
  if (!el) return;
  if (!tag || el.tag === tag) yield el;
  for (const c of el.children) yield* iter(c, tag);
}

// ElementTree-like paths: "a/b" (children), ".//a" (first descendant), ".//a/b"
function findAll(el: El | null | undefined, path: string): El[] {
  if (!el) return [];
  let nodes: El[] = [el];
  let parts = path.split("/");
  if (path.startsWith(".//")) {
    parts = path.slice(3).split("/");
    nodes = [...iter(el)].filter((n) => n !== el && n.tag === parts[0]);
    parts = parts.slice(1);
  }
  for (const part of parts) {
    if (part === "" || part === ".") continue;
    nodes = nodes.flatMap((n) => n.children.filter((c) => c.tag === part));
  }
  return nodes;
}
const find = (el: El | null | undefined, path: string): El | null => findAll(el, path)[0] ?? null;
const textOf = (el: El | null | undefined) => (el ? el.text.trim() : "");
const text = (el: El | null | undefined, path: string) => textOf(find(el, path));

function amount(el: El | null | undefined, path: string): { value: number; currency: string } | null {
  const f = find(el, path);
  const t = textOf(f);
  if (!f || !t) return null;
  const value = Number(t);
  return Number.isFinite(value) ? { value, currency: f.attrs.currencyID ?? "" } : null;
}

function num(value: string | null | undefined): number | null {
  if (value == null) return null;
  let raw = value.replace(/[\s ]/g, "");
  if (!raw) return null;
  if (raw.includes(",") && raw.includes(".")) {
    raw = raw.lastIndexOf(",") > raw.lastIndexOf(".") ? raw.replace(/\./g, "").replace(",", ".") : raw.replace(/,/g, "");
  } else if (raw.includes(",")) raw = raw.replace(/,/g, ".");
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

const alpha3 = (code?: string | null) => {
  const c = (code ?? "").trim().toUpperCase();
  if (!c) return null;
  return c.length === 3 ? c : ALPHA3[c] ?? c;
};

function paragraphs(el: El | null): string {
  if (!el) return "";
  const parts = [...iter(el, "P")].map((p) => p.text.split(/\s+/).filter(Boolean).join(" ")).filter(Boolean);
  return parts.join(" ") || el.text.split(/\s+/).filter(Boolean).join(" ");
}

function clip(value: string | null | undefined, limit: number): string | null {
  const v = (value ?? "").split(/\s+/).filter(Boolean).join(" ");
  if (!v) return null;
  return v.length <= limit ? v : v.slice(0, limit - 1).trimEnd() + "…";
}

const date10 = (v: string) => (/^\d{4}-\d{2}-\d{2}/.test(v.trim()) ? v.trim().slice(0, 10) : null);

function dmy(el: El | null): string | null {
  if (!el) return null;
  const d = text(el, "DAY"), m = text(el, "MONTH"), y = text(el, "YEAR");
  if (!(d && m && y)) return null;
  return `${y.padStart(4, "0")}-${m.padStart(2, "0")}-${d.padStart(2, "0")}`;
}

const isDigits = (s: string) => /^\d+$/.test(s);
const round2 = (x: number) => Math.round(x * 100) / 100;

// ------------------------------------------------------------------ eForms

function parseEforms(root: El, number: string): Row[] {
  const orgs: Record<string, Row> = {};
  for (const org of iter(root, "Organization")) {
    const company = find(org, "Company");
    if (!company) continue;
    orgs[text(company, "PartyIdentification/ID")] = {
      name: text(company, "PartyName/Name"),
      identifier: text(company, "PartyLegalEntity/CompanyID") || null,
      country: alpha3(text(company, "PostalAddress/Country/IdentificationCode")),
      size: text(company, "CompanySizeCode") || null,
    };
  }

  const lots: Record<string, Row> = {};
  let nLots = 0;
  for (const lot of findAll(root, "ProcurementProjectLot")) {
    const idEl = find(lot, "ID");
    if (!idEl || (idEl.attrs.schemeName ?? "Lot") !== "Lot") continue;
    nLots++;
    const project = find(lot, "ProcurementProject");
    const cpv = [text(project, "MainCommodityClassification/ItemClassificationCode"),
      ...findAll(project, "AdditionalCommodityClassification/ItemClassificationCode").map(textOf)];
    const quantity = find(project, "EstimatedOverallContractQuantity");
    const fwCodes = [...iter(lot, "ContractingSystemTypeCode")].filter((c) => c.attrs.listName === "framework-agreement").map(textOf);
    const est = amount(project, "RequestedTenderTotal/EstimatedOverallContractAmount");
    const fwMax = amount(project, "RequestedTenderTotal/EstimatedOverallFrameworkContractsAmount");
    lots[textOf(idEl)] = {
      title: clip(text(project, "Name"), TITLE_CHARS),
      description: clip(text(project, "Description"), DESCRIPTION_CHARS),
      cpv_codes: [...new Set(cpv.filter(Boolean))],
      quantity: quantity ? num(quantity.text) : null,
      quantity_unit: quantity ? quantity.attrs.unitCode || null : null,
      estimated_value: est?.value ?? null,
      estimated_currency: est?.currency || null,
      framework: fwCodes.some((c) => c.startsWith("fa-")),
      framework_max: fwMax?.value ?? null,
      framework_currency: fwMax?.currency || null,
    };
  }

  const result = find(root, ".//NoticeResult");
  if (!result) return [];
  const noticeTotal = amount(result, "TotalAmount");
  const parties: Record<string, string[]> = {};
  const groupParties = new Set<string>();
  for (const p of findAll(result, "TenderingParty")) {
    const members = findAll(p, "Tenderer").map((t) => text(t, "ID"));
    parties[text(p, "ID")] = members;
    if (members.length > 1) groupParties.add(text(p, "ID"));
  }
  const tenders: Record<string, El> = {};
  for (const t of findAll(result, "LotTender")) tenders[text(t, "ID")] = t;
  const contracted: Record<string, El> = {};
  for (const c of findAll(result, "SettledContract")) for (const ref of findAll(c, "LotTender")) contracted[text(ref, "ID")] = c;
  const hasContracts = Object.keys(contracted).length > 0;
  const lotResults = findAll(result, "LotResult");
  const awardedResults = lotResults.filter((r) => ["selec-w", ""].includes(text(r, "TenderResultCode")));

  const records: Row[] = [];
  const seen: Record<string, number> = {};
  for (const lr of lotResults) {
    const lotId = text(lr, "TenderLot/ID");
    const code = text(lr, "TenderResultCode") || null;
    const listed = findAll(lr, "LotTender").map((t) => tenders[text(t, "ID")]).filter(Boolean);
    const winning = hasContracts ? listed.filter((t) => text(t, "ID") in contracted) : listed;
    const confirmed = hasContracts || listed.length <= 1;
    const awarded = (code === "selec-w" || code === null) && winning.length > 0;

    const winners: Row[] = [];
    const values: { value: number; currency: string }[] = [];
    const awardDates: (string | null)[] = [], contractDates: (string | null)[] = [];
    for (const tender of awarded ? winning : []) {
      const party = text(tender, "TenderingParty/ID");
      const value = amount(tender, "LegalMonetaryTotal/PayableAmount");
      if (value) values.push(value);
      const contract = contracted[text(tender, "ID")];
      if (contract) {
        awardDates.push(date10(text(contract, "AwardDate")));
        contractDates.push(date10(text(contract, "IssueDate")));
      }
      for (const member of parties[party] ?? []) {
        const org = orgs[member] ?? {};
        if (!org.name) continue;
        winners.push({ ...org, tender: text(tender, "ID"), party, group: groupParties.has(party),
          tender_value: value?.value ?? null, tender_currency: value?.currency ?? null });
      }
    }

    let lotValue: number | null = null, currency: string | null = null, valueSource: string | null = null;
    const currencies = new Set(values.map((v) => v.currency));
    if (awarded && values.length && currencies.size === 1 && values.length === winning.length) {
      lotValue = round2(values.reduce((s, v) => s + v.value, 0));
      currency = values[0].currency;
      valueSource = "tender_value";
    } else if (awarded && !values.length && noticeTotal && awardedResults.length === 1 && winning.length === 1) {
      lotValue = noticeTotal.value;
      currency = noticeTotal.currency;
      valueSource = "notice_total_one_lot";
    }
    let bids: number | null = null;
    for (const s of findAll(lr, "ReceivedSubmissionsStatistics")) {
      if (text(s, "StatisticsCode") === "tenders" && isDigits(text(s, "StatisticsNumeric"))) bids = Number(text(s, "StatisticsNumeric"));
    }
    const fwMax = amount(lr, "FrameworkAgreementValues/MaximumValueAmount");
    let key = lotId || text(lr, "ID") || `RES-${records.length}`;
    seen[key] = (seen[key] ?? 0) + 1;
    if (seen[key] > 1) key = `${key}/${text(lr, "ID") || seen[key]}`;
    const base = lots[lotId] ?? {};
    records.push({
      publication_number: number, format: "eforms", lot_key: key, lot_id: lotId || null, n_lots_in_notice: nLots,
      title: base.title ?? null, description: base.description ?? null, cpv_codes: base.cpv_codes ?? [],
      quantity: base.quantity ?? null, quantity_unit: base.quantity_unit ?? null,
      estimated_value: base.estimated_value ?? null, estimated_currency: base.estimated_currency ?? null,
      framework: base.framework ?? false,
      framework_max: fwMax?.value || base.framework_max || null,   // a 0 maximum counts as absent (as in Python)
      framework_currency: fwMax?.currency || base.framework_currency || null,
      result_code: code, awarded, winner_confirmed: confirmed, n_winning_tenders: awarded ? winning.length : 0,
      winners, value: lotValue, currency, value_source: valueSource, bids_received: bids,
      award_date: awardDates.find(Boolean) ?? null, contract_date: contractDates.find(Boolean) ?? null,
    });
  }
  return records;
}

// ------------------------------------------------------------------ legacy TED_EXPORT (R2.0.8 / R2.0.9)

function legacyContractor(el: El): Row | null {
  const official = [...iter(el, "OFFICIALNAME")][0];
  const org = official?.parent;
  if (!org) return null;
  const name = text(org, "OFFICIALNAME").split(/\s+/).filter(Boolean).join(" ");
  if (!name) return null;
  const country = [...iter(el, "COUNTRY")][0];
  const sme = find(el, "SME") ? true : find(el, "NO_SME") ? false : null;
  return { name, identifier: text(org, "NATIONALID") || null, country: country ? alpha3(country.attrs.VALUE) : null, sme };
}

function parseLegacy(root: El, number: string): Row[] {
  const section = find(root, "FORM_SECTION");
  if (!section || !section.children.length) return [];
  const form = section.children.find((f) => f.attrs.CATEGORY === "ORIGINAL") ?? section.children[0];
  const noticeCpv = [...iter(root, "ORIGINAL_CPV")].map((c) => c.attrs.CODE).filter(Boolean);
  const framework = [...iter(form)].some((e) => e.tag.includes("FRAMEWORK") || Object.values(e.attrs).join(" ").includes("FRAMEWORK"));

  const lotInfo: Record<string, Row> = {};
  for (const d of iter(form, "OBJECT_DESCR")) {
    const lotNo = text(d, "LOT_NO") || d.attrs.ITEM || "";
    const est = find(d, "VAL_OBJECT");
    lotInfo[lotNo] = {
      title: clip(paragraphs(find(d, "TITLE")), TITLE_CHARS),
      description: clip(paragraphs(find(d, "SHORT_DESCR")), DESCRIPTION_CHARS),
      cpv_codes: [...iter(d, "CPV_CODE")].map((c) => c.attrs.CODE).filter(Boolean),
      estimated_value: est ? num(est.text) : null,
      estimated_currency: est ? est.attrs.CURRENCY ?? null : null,
    };
  }
  const nLots = Math.max(Object.keys(lotInfo).length, 1);
  const noticeTitle = clip(paragraphs(find(form, ".//TITLE_CONTRACT")) || paragraphs(find(form, "OBJECT_CONTRACT/TITLE")), TITLE_CHARS);
  const noticeDescr = clip(paragraphs(find(form, ".//SHORT_CONTRACT_DESCRIPTION")) || paragraphs(find(form, "OBJECT_CONTRACT/SHORT_DESCR")), DESCRIPTION_CHARS);

  const fmt = find(form, ".//AWARD_CONTRACT") ? "legacy_2014" : "legacy_r208";
  const items = fmt === "legacy_2014" ? findAll(form, "AWARD_CONTRACT") : [...iter(form)].filter((e) => e.tag.startsWith("AWARD_OF_CONTRACT"));
  const records: Row[] = [];
  items.forEach((item, i) => {
    let lotNo: string, title: string | null, awarded: boolean, resultCode: string | null, contractors: Row[], group: boolean;
    let bids: string, value: number | null = null, valueCur: string | null = null, est: number | null = null, estCur: string | null = null;
    let awardDate: string | null;
    if (fmt === "legacy_2014") {
      lotNo = text(item, "LOT_NO");
      title = clip(paragraphs(find(item, "TITLE")), TITLE_CHARS);
      const awardedEl = find(item, "AWARDED_CONTRACT");
      const notAwarded = find(item, "NO_AWARDED_CONTRACT");
      awarded = !!awardedEl;
      resultCode = awarded ? "awarded" : notAwarded && find(notAwarded, ".//PROCUREMENT_DISCONTINUED") ? "discontinued" : notAwarded ? "unsuccessful" : null;
      const scope = awardedEl ?? item;
      contractors = [...iter(scope, "CONTRACTOR")].map(legacyContractor).filter(Boolean) as Row[];
      group = !!find(scope, ".//AWARDED_TO_GROUP");
      bids = text(scope, ".//NB_TENDERS_RECEIVED");
      const v = find(scope, ".//VAL_TOTAL");
      if (v) { value = num(v.attrs.FMTVAL ?? v.text); valueCur = v.attrs.CURRENCY || null; }
      const e = find(scope, ".//VAL_ESTIMATED_TOTAL");
      if (e) { est = num(e.attrs.FMTVAL ?? e.text); estCur = e.attrs.CURRENCY || null; }
      awardDate = date10(text(scope, "DATE_CONCLUSION_CONTRACT"));
    } else {
      lotNo = text(item, "LOT_NUMBER");
      title = clip(paragraphs(find(item, "CONTRACT_TITLE")), TITLE_CHARS);
      contractors = [...iter(item, "ECONOMIC_OPERATOR_NAME_ADDRESS")].map(legacyContractor).filter(Boolean) as Row[];
      awarded = contractors.length > 0;
      resultCode = awarded ? "awarded" : null;
      group = contractors.length > 1;
      bids = text(item, "OFFERS_RECEIVED_NUMBER");
      const info = find(item, "CONTRACT_VALUE_INFORMATION");
      const cost = find(info, "COSTS_RANGE_AND_CURRENCY_WITH_VAT_RATE");
      const vc = find(cost, "VALUE_COST");
      if (cost && vc) { value = num(vc.attrs.FMTVAL ?? vc.text); valueCur = cost.attrs.CURRENCY || null; }
      const estParent = find(info, "INITIAL_ESTIMATED_TOTAL_VALUE_CONTRACT");
      const ev = find(estParent, ".//VALUE_COST");
      if (estParent && ev) {
        est = num(ev.attrs.FMTVAL ?? ev.text);
        estCur = estParent.attrs.CURRENCY || [...iter(estParent)].find((x) => x.attrs.CURRENCY)?.attrs.CURRENCY || null;
      }
      awardDate = dmy(find(item, "CONTRACT_AWARD_DATE"));
    }
    const info = lotNo ? lotInfo[lotNo] ?? {} : Object.keys(lotInfo).length === 1 ? Object.values(lotInfo)[0] : {};
    records.push({
      publication_number: number, format: fmt, lot_key: `item-${item.attrs.ITEM || i + 1}`, lot_id: lotNo || null,
      n_lots_in_notice: nLots,
      title: title || info.title || (nLots === 1 ? noticeTitle : null),
      description: info.description || (nLots === 1 ? noticeDescr : null),
      cpv_codes: [...new Set([...((info.cpv_codes as string[]) ?? []), ...noticeCpv])],
      quantity: null, quantity_unit: null,
      estimated_value: info.estimated_value ?? est, estimated_currency: info.estimated_currency ?? estCur,
      framework, framework_max: null, framework_currency: null,
      result_code: resultCode, awarded, winner_confirmed: true,
      n_winning_tenders: awarded && contractors.length ? 1 : 0,
      winners: awarded ? contractors.map((c) => ({ ...c, group })) : [],
      value: awarded ? value : null, currency: awarded ? valueCur : null,
      value_source: awarded && value !== null ? "legacy_contract_value" : null,
      bids_received: isDigits(bids) ? Number(bids) : null,
      award_date: awardDate, contract_date: fmt === "legacy_2014" ? awardDate : null,
    });
  });
  return records;
}

export function parseNotice(xml: string, number: string): { kind: string; lots: Row[] } {
  const root = parseXml(xml);
  if (!root) return { kind: "unparseable", lots: [] };
  if (root.tag === "TED_EXPORT") return { kind: "legacy", lots: parseLegacy(root, number) };
  if (["ContractAwardNotice", "ContractNotice", "PriorInformationNotice"].includes(root.tag)) {
    return { kind: "eforms", lots: parseEforms(root, number) };
  }
  return { kind: `unknown:${root.tag}`, lots: [] };
}

// ------------------------------------------------------------------ database + run

function headers(db: Db, extra: Record<string, string> = {}): Record<string, string> {
  const auth: Record<string, string> = db.key.startsWith("eyJ") ? { Authorization: `Bearer ${db.key}` } : {};
  return { apikey: db.key, ...auth, "Content-Type": "application/json", ...extra };
}

async function rest(db: Db, path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${db.url}/rest/v1/${path}`, { ...init, headers: headers(db, (init.headers ?? {}) as Record<string, string>) });
  if (!response.ok) throw new Error(`${path.split("?")[0]} ${response.status}: ${redact((await response.text()).slice(0, 300))}`);
  return response;
}

export async function checkSecret(db: Db, candidate: string | null): Promise<boolean> {
  if (!candidate) return false;
  try {
    const r = await rest(db, "rpc/kb_baseline_check_secret", { method: "POST", body: JSON.stringify({ candidate }) });
    return (await r.json()) === true;
  } catch {
    return false;
  }
}

type LotsState = { sample: string[]; processed: string[]; failed: string[]; requests: number; lots: number;
  formats: Record<string, number>; done: boolean; started_at?: string; finished_at?: string; runs: number; updated_at?: string };

async function loadSnapshot(db: Db, snapshot: string) {
  const rows = await (await rest(db, `kb_snapshots?id=eq.${encodeURIComponent(snapshot)}&select=status,harvest_state`)).json();
  if (!rows.length) throw new Error(`unknown snapshot ${snapshot}`);
  return rows[0] as { status: string; harvest_state: Record<string, unknown> };
}

async function saveState(db: Db, snapshot: string, harvest: Record<string, unknown>, lots: LotsState) {
  lots.updated_at = new Date().toISOString();
  await rest(db, `kb_snapshots?id=eq.${encodeURIComponent(snapshot)}`, {
    method: "PATCH", headers: { Prefer: "return=minimal" }, body: JSON.stringify({ harvest_state: { ...harvest, lots } }),
  });
}

export async function status(db: Db, snapshot: string) {
  const s = await loadSnapshot(db, snapshot);
  const lots = (s.harvest_state?.lots ?? {}) as Partial<LotsState>;
  return { snapshot, status: s.status, sample: lots.sample?.length ?? 0, processed: lots.processed?.length ?? 0,
    failed: lots.failed ?? [], lots: lots.lots ?? 0, requests: lots.requests ?? 0, done: lots.done ?? false, updated_at: lots.updated_at };
}

let lastRequest = 0;
async function fetchXml(number: string, deadline: number): Promise<string | null> {
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    const wait = MIN_INTERVAL_MS - (Date.now() - lastRequest);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    lastRequest = Date.now();
    try {
      const response = await fetch(XML_URL(number), { headers: { "User-Agent": "salespatriot-baseline-lots/1.0" } });
      if (response.status === 404) return null;
      if (response.ok) {
        const body = await response.text();
        if (body.trimStart().startsWith("<")) return body;
      }
    } catch (_) { /* retry */ }
    const delay = Math.min(30000, 2000 * 2 ** attempt);
    if (Date.now() + delay > deadline) break;
    await new Promise((r) => setTimeout(r, delay));
  }
  throw new Error(`fetch failed: ${number}`);
}

export async function runLots(db: Db, snapshot: string, budgetSeconds: number) {
  const deadline = Date.now() + budgetSeconds * 1000;
  const snap = await loadSnapshot(db, snapshot);
  if (snap.status !== "building") throw new Error(`snapshot ${snapshot} is ${snap.status} (must be building)`);
  const harvest = snap.harvest_state ?? {};
  const state = harvest.lots as LotsState | undefined;
  if (!state?.sample?.length) throw new Error("harvest_state.lots.sample is empty");
  state.processed ??= []; state.failed ??= []; state.requests ??= 0; state.lots ??= 0; state.formats ??= {}; state.runs = (state.runs ?? 0) + 1;
  state.started_at ??= new Date().toISOString();
  const done = new Set(state.processed);
  const pending = state.sample.filter((n) => !done.has(n));
  let processedNow = 0, lotsNow = 0;
  const errors: string[] = [];
  for (const number of pending) {
    if (Date.now() > deadline - 5000) break;
    try {
      const xml = await fetchXml(number, deadline);
      state.requests++;
      const { kind, lots } = xml ? parseNotice(xml, number) : { kind: "missing", lots: [] };
      if (lots.length) {
        await rest(db, "kb_baseline_lots?on_conflict=snapshot_id,publication_number,lot_key", {
          method: "POST", headers: { Prefer: "resolution=merge-duplicates,return=minimal" },
          body: JSON.stringify(lots.map((l) => ({ snapshot_id: snapshot, ...l }))),
        });
      }
      state.formats[kind] = (state.formats[kind] ?? 0) + 1;
      state.lots += lots.length; lotsNow += lots.length;
      state.processed.push(number); processedNow++;
      state.failed = state.failed.filter((f) => f !== number);
    } catch (error) {
      errors.push(redact(`${number}: ${String(error)}`));
      if (!state.failed.includes(number)) state.failed.push(number);
    }
    if (processedNow % 20 === 0) await saveState(db, snapshot, harvest, state);
  }
  state.done = state.processed.length >= state.sample.length;
  if (state.done) state.finished_at ??= new Date().toISOString();
  await saveState(db, snapshot, harvest, state);
  return { snapshot, processed_now: processedNow, lots_now: lotsNow, processed: state.processed.length,
    sample: state.sample.length, lots: state.lots, done: state.done, errors: errors.slice(0, 10) };
}
