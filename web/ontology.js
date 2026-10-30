/* Product ontology layer: product families, calibres, regulatory codes and family-based tender matches,
 * read from Supabase through PostgREST with the public (read-only) key in config.js.
 *
 * Optional by design. Every call fails soft: a timeout, an HTTP error (for example a ui_* view that is not
 * deployed yet) or an empty answer leaves the caches empty, and the pages render as they do without it.
 * Answers are cached (failures are retried after FAIL_TTL, answers refreshed after OK_TTL because the
 * classifier fills data in over time). onChange() listeners run, debounced, whenever new data arrives,
 * so pages can redraw just the parts that show ontology data.
 *
 * API views and functions (owned by the database side): product_family_tree, product_calibres, ui_items,
 * ui_supplier_families, ui_tender_families, rpc ui_match_tender, rpc ui_regulatory_profile.
 */
(function () {
  "use strict";

  const cfg = window.SalesPatriotConfig || {};
  const BASE = String(cfg.SUPABASE_URL || "").replace(/\/+$/, "");
  const KEY = cfg.SUPABASE_ANON_KEY || "";
  // ?no-ontology turns the layer off, handy to compare with the static-only UI.
  const enabled = Boolean(BASE && KEY && window.fetch && window.AbortController) &&
    !new URLSearchParams(location.search).has("no-ontology");
  const TIMEOUT_MS = 8000;
  const OK_TTL = 5 * 60 * 1000;
  const FAIL_TTL = 30 * 1000;
  const MAX_PARALLEL = 4;
  const CHUNK = 40; // values per in.(...) filter, keeps URLs short

  // product_items.source_table values for the two supplier-page sources (see the merge contract).
  const SRC_CATALOGUE = "suppliers.catalogue";
  const SRC_CONTRACT = "supplier_contracts";

  const data = {
    families: new Map(),         // id → product_family_tree row
    calibres: new Map(),         // code → product_calibres row
    supplierFamilies: new Map(), // supplier_id → ui_supplier_families rows
    tenderFamilies: new Map(),   // publication_number → ui_tender_families rows
    items: new Map(),            // supplier_id → { catalogue: Map(index → row), contracts: Map(key → row) }
    matches: new Map(),          // publication_number → ui_match_tender rows
  };
  const cache = new Map();       // request key → { at, ok, promise }
  const listeners = new Set();
  let available = false;         // true once any ontology request returned data

  /* ---------- requests ---------- */
  let running = 0;
  const queue = [];
  function pump() {
    while (running < MAX_PARALLEL && queue.length) {
      running += 1;
      queue.shift()().finally(() => { running -= 1; pump(); });
    }
  }
  /** GET (no body) or POST (body) to /rest/v1/<path>; resolves to the parsed JSON or null, never rejects. */
  function request(path, body) {
    return new Promise((resolve) => {
      queue.push(async () => {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
        try {
          const res = await fetch(`${BASE}/rest/v1/${path}`, {
            method: body ? "POST" : "GET",
            headers: { apikey: KEY, Authorization: `Bearer ${KEY}`, Accept: "application/json",
              ...(body ? { "Content-Type": "application/json" } : {}) },
            body: body ? JSON.stringify(body) : undefined,
            signal: ctrl.signal,
          });
          resolve(res.ok ? await res.json() : null);
        } catch {
          resolve(null);
        } finally {
          clearTimeout(timer);
        }
      });
      pump();
    });
  }
  const rows = (value) => (Array.isArray(value) ? value : null);
  const view = (name, params) => request(`${name}?${new URLSearchParams(params)}`).then(rows);
  const rpc = (fn, args) => request(`rpc/${fn}`, args);
  const inList = (values) => `in.(${values.map((v) => `"${String(v).replace(/["\\]/g, "\\$&")}"`).join(",")})`;
  const chunks = (list) => Array.from({ length: Math.ceil(list.length / CHUNK) }, (_, i) => list.slice(i * CHUNK, i * CHUNK + CHUNK));

  /** One request per key at a time; the answer is kept for OK_TTL, a failure (null) for FAIL_TTL. */
  const fresh = (hit) => hit && (hit.ok === null || Date.now() - hit.at < (hit.ok ? OK_TTL : FAIL_TTL));
  function track(fetcher) {
    const entry = { at: Date.now(), ok: null, promise: null };
    entry.promise = fetcher().catch(() => null).then((value) => {
      entry.ok = value != null;
      entry.at = Date.now();
      return value;
    });
    return entry;
  }
  function cached(key, fetcher) {
    if (!enabled) return Promise.resolve(null);
    const hit = cache.get(key);
    if (fresh(hit)) return hit.promise;
    const entry = track(fetcher);
    cache.set(key, entry);
    return entry.promise;
  }

  let notifyTimer = 0;
  function changed() {
    if (notifyTimer) return;
    notifyTimer = setTimeout(() => {
      notifyTimer = 0;
      listeners.forEach((fn) => { try { fn(); } catch (err) { console.warn("Ontology listener failed:", err); } });
    }, 120);
  }
  function got(list) {
    if (list && list.length) available = true;
    changed();
  }

  /* ---------- loaders ---------- */
  function loadTree() {
    return cached("tree", () => view("product_family_tree", { select: "*", order: "sort_order" }).then((list) => {
      if (list) { for (const f of list) data.families.set(f.id, f); got(list); }
      return list;
    }));
  }

  function loadCalibres() {
    return cached("calibres", () => view("product_calibres", { select: "code,label,kind,bore_mm" }).then((list) => {
      if (list) { for (const c of list) data.calibres.set(c.code, c); changed(); }
      return list;
    }));
  }

  const byEvidence = (a, b) => (b.proven_lots || 0) - (a.proven_lots || 0) || (b.declared_items || 0) - (a.declared_items || 0) ||
    String(a.family_name || "").localeCompare(String(b.family_name || ""));

  function loadSupplierFamilies() {
    return cached("supplier-families", () => view("ui_supplier_families", { select: "*" }).then((list) => {
      if (list) {
        data.supplierFamilies.clear();
        for (const r of list) {
          if (!data.supplierFamilies.has(r.supplier_id)) data.supplierFamilies.set(r.supplier_id, []);
          data.supplierFamilies.get(r.supplier_id).push(r);
        }
        for (const l of data.supplierFamilies.values()) l.sort(byEvidence);
        got(list);
      }
      return list;
    }));
  }

  /** Families of the given tenders (publication numbers), fetched in chunks. */
  function loadTenderFamilies(publicationNumbers) {
    const pubs = [...new Set((publicationNumbers || []).filter(Boolean))].sort();
    return Promise.all(chunks(pubs).map((chunk) => cached(`tenders:${chunk.join(",")}`, () =>
      view("ui_tender_families", { select: "*", publication_number: inList(chunk) }).then((list) => {
        if (list) {
          for (const pub of chunk) data.tenderFamilies.set(pub, []);
          for (const r of list) data.tenderFamilies.get(r.publication_number)?.push(r);
          for (const l of data.tenderFamilies.values()) l.sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
          got(list);
        }
        return list;
      })))).then(() => available);
  }

  /** Classified items (ui_items) of one or more suppliers, so supplier pages can show family chips.
   *  Cached per supplier; suppliers not loaded yet are fetched together. */
  function loadSupplierItems(supplierIds) {
    if (!enabled) return Promise.resolve(false);
    const ids = [...new Set([].concat(supplierIds || []).filter(Boolean))].sort();
    const waits = [];
    const todo = [];
    for (const id of ids) {
      const hit = cache.get(`items:${id}`);
      if (fresh(hit)) waits.push(hit.promise);
      else todo.push(id);
    }
    for (const chunk of chunks(todo)) {
      const entry = track(() => view("ui_items", { select: "*", supplier_id: inList(chunk) }).then((list) => {
        if (list) { indexItems(chunk, list); got(list); }
        return list;
      }));
      for (const id of chunk) cache.set(`items:${id}`, entry);
      waits.push(entry.promise);
    }
    return Promise.all(waits).then(() => available);
  }

  const contractKey = (notice, lot) => `${notice}/${lot || "notice"}`;
  function indexItems(supplierIds, list) {
    for (const id of supplierIds) data.items.set(id, { catalogue: new Map(), contracts: new Map() });
    for (const r of list) {
      const bucket = data.items.get(r.supplier_id);
      if (!bucket) continue;
      if (r.source_table === SRC_CATALOGUE) {
        // source_pk = <supplier_id>/<index>, source_index = index
        const index = r.source_index ?? Number(String(r.source_pk || "").split("/").pop());
        if (Number.isInteger(index)) bucket.catalogue.set(index, r);
      } else if (r.source_table === SRC_CONTRACT) {
        // source_pk = <supplier_id>/<notice_number>/<lot_id or 'notice'>
        if (r.source_pk) bucket.contracts.set(r.source_pk, r);
        if (r.publication_number) bucket.contracts.set(contractKey(r.publication_number, r.lot_id), r);
      }
    }
  }

  /** Load what every page can use: the family tree, calibre labels, supplier families and tender families. */
  function load(opts = {}) {
    if (!enabled) return Promise.resolve(false);
    return Promise.all([loadTree(), loadCalibres(), loadSupplierFamilies(), loadTenderFamilies(opts.publicationNumbers)])
      .then(() => available, () => available);
  }

  /* ---------- lookups (synchronous, from the caches; empty until data has arrived) ---------- */
  const familiesForSupplier = (supplierId) => data.supplierFamilies.get(supplierId) || [];
  const familiesForTender = (publicationNumber) => data.tenderFamilies.get(publicationNumber) || [];
  const family = (id) => data.families.get(id) || null;
  const calibre = (code) => data.calibres.get(code) || null;
  const calibreLabel = (code) => (code ? calibre(code)?.label || String(code) : "");
  const same = (a, b) => String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();

  /** ui_items row for suppliers.catalogue.items[index]; `product` guards against a catalogue re-ordered since the sync. */
  function itemForCatalogue(supplierId, index, product) {
    const bucket = data.items.get(supplierId);
    if (!bucket) return null;
    const row = bucket.catalogue.get(index);
    if (row && (!product || !row.title || same(row.title, product))) return row;
    if (product) for (const r of bucket.catalogue.values()) if (same(r.title, product)) return r;
    return null;
  }

  /** ui_items row for one supplier contract (lot) from supplier_contracts. */
  function itemForContract(supplierId, noticeNumber, lotId) {
    const bucket = data.items.get(supplierId);
    if (!bucket) return null;
    return bucket.contracts.get(`${supplierId}/${contractKey(noticeNumber, lotId)}`) ||
      bucket.contracts.get(contractKey(noticeNumber, lotId)) || null;
  }

  /** Last ui_match_tender answer for a tender, or undefined when it has not arrived (yet). */
  const cachedMatch = (publicationNumber) => data.matches.get(publicationNumber);

  /* ---------- API calls ---------- */
  /** Companies that can supply a tender, best first (ui_match_tender). [] when unavailable. */
  async function matchTender(publicationNumber, limit = 10) {
    if (!enabled || !publicationNumber) return [];
    const list = await cached(`match:${publicationNumber}:${limit}`, () =>
      rpc("ui_match_tender", { p_publication_number: publicationNumber, p_limit: limit }).then(rows).then((answer) => {
        if (answer) {
          data.matches.set(publicationNumber, answer);
          // Matching needs each matched supplier's classified catalogue to pick the product to negotiate.
          const ids = answer.map((r) => r.supplier_id).filter(Boolean);
          if (ids.length) loadSupplierItems(ids);
          got(answer);
        }
        return answer;
      }));
    return list || [];
  }

  /** EU Military List, NATO FSC, HS/CN, ADR… codes for a family, with item rules when a calibre is given. */
  async function regulatory(familyId, calibreCode) {
    if (!enabled || !familyId) return null;
    const value = await cached(`regulatory:${familyId}|${calibreCode || ""}`, () =>
      rpc("ui_regulatory_profile", { p_family_id: familyId, p_calibre: calibreCode || null }));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  function onChange(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  window.Ontology = {
    enabled, load, loadTenderFamilies, loadSupplierItems,
    familiesForSupplier, familiesForTender, itemForCatalogue, itemForContract, cachedMatch,
    matchTender, regulatory, family, calibre, calibreLabel, onChange,
    get available() { return available; },
  };
})();
