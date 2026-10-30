/* Agent matching and conversation scripts.
 *
 * Conversations are simulated but grounded in the profile data: the buyer's
 * real TED tender, the supplier's catalogue item and its TED track record, and
 * (when one exists) the unit price awarded in a real TED contract as the
 * negotiation anchor. Quantities, mandates and peer references are fictitious
 * and every conversation says so.
 */
(function () {
  "use strict";

  const SINGLE_CALIBRES = {
    "5.56": "5.56x45", "7.62": "7.62x51", "9": "9x19", "12.7": "12.7x99",
    "5.45": "5.45x39", "4.6": "4.6x30", "7.65": "7.65x17", "8.6": "8.6x70",
  };

  /** Normalised calibre keys mentioned in a text, e.g. {"5.56x45", "9x19"}. */
  function calibreKeys(text) {
    const keys = new Set();
    if (!text) return keys;
    const t = text.replace(/(\d),(\d)/g, "$1.$2");
    for (const m of t.matchAll(/(\d{1,3}(?:\.\d{1,2})?)\s*(?:mm)?\s*[x×]\s*(\d{2,3})/gi)) {
      keys.add(`${m[1]}x${m[2]}`);
    }
    for (const m of t.matchAll(/(?:^|[^\d.])(\d{1,2}(?:\.\d{1,2})?)\s*mm/gi)) {
      if (SINGLE_CALIBRES[m[1]]) keys.add(SINGLE_CALIBRES[m[1]]);
    }
    if (/\.308|308\s*W/i.test(t)) keys.add("7.62x51");
    if (/\.338|338\s*LM/i.test(t)) keys.add("8.6x70");
    if (/\.50\s*BMG/i.test(t)) keys.add("12.7x99");
    if (/\.22\s*L\.?\s*R/i.test(t)) keys.add(".22lr");
    if (/12\s*\/\s*70|12[- ]?gauge|12 ga/i.test(t)) keys.add("12ga");
    return keys;
  }

  const isWeapons = (cpv) => String(cpv || "").startsWith("353");

  function itemText(item) {
    return [item.product, item.brand_model, ...Object.values(item.specs || {})].join(" ");
  }

  function parsePrice(text) {
    const m = /(EUR|RON|DKK|BGN|USD|SEK|PLN|CZK|GBP|NOK)\s*([\d,]+(?:\.\d+)?)/.exec(text || "");
    if (!m) return null;
    return { currency: m[1], value: parseFloat(m[2].replace(/,/g, "")) };
  }

  // Same product family: ammunition (3533) must not match a rifle part (3534) of the same calibre.
  // Items often carry the generic 35300000 code, so their catalogue category decides.
  const CATEGORY_FAMILY = {
    "Ammunition": "3533", "Firearms": "3532",
    "Parts of firearms and ammunition": "3534", "Miscellaneous weapons": "3531",
  };
  function sameFamily(codes, item) {
    const family = CATEGORY_FAMILY[item.category] || item.cpv.slice(0, 4);
    // A tender's generic 35300000 code only counts when it has nothing more specific.
    const specific = codes.filter((c) => isWeapons(c) && c !== "35300000");
    return specific.length ? specific.some((c) => c.slice(0, 4) === family) : codes.includes("35300000");
  }

  function scoreMatch(notice, item, supplier) {
    const codes = notice.cpv_codes || [];
    if (!isWeapons(item.cpv) || !codes.some(isWeapons) || !sameFamily(codes, item)) return 0;
    const demand = calibreKeys(`${notice.title} ${notice.description}`);
    const offer = calibreKeys(itemText(item));
    if (![...demand].some((k) => offer.has(k))) return 0;
    let score = 5;
    const status = item.evidence.verification_status;
    if (status === "document_verified") score += 3;
    else if (status === "inferred") return 0;
    if (parsePrice(item.commercial.derived_unit_price)) score += 3;
    // Clean cases first so the lead demo shows an autonomous close; flagged ones still appear.
    if (supplier.capabilities?.risk_flags?.length) score -= 2;
    return score;
  }

  /** Best (notice, item) match for every buyer/supplier pair, best first. */
  function findMatches(buyers, suppliers) {
    const best = new Map();
    for (const buyer of buyers) {
      for (const notice of buyer.recent_notices || []) {
        for (const supplier of suppliers) {
          for (const item of supplier.catalogue?.items || []) {
            const score = scoreMatch(notice, item, supplier);
            if (!score) continue;
            const key = `${buyer.id}|${supplier.id}`;
            const prev = best.get(key);
            if (!prev || score > prev.score || (score === prev.score && notice.published > prev.notice.published)) {
              best.set(key, { score, buyer, supplier, notice, item });
            }
          }
        }
      }
    }
    return [...best.values()].sort((a, b) => b.score - a.score);
  }

  /** Best supplier item for one tender, or null. */
  function bestForNotice(buyer, notice, suppliers) {
    let best = null;
    for (const supplier of suppliers) {
      for (const item of supplier.catalogue?.items || []) {
        const score = scoreMatch(notice, item, supplier);
        if (score && (!best || score > best.score)) best = { score, buyer, supplier, notice, item };
      }
    }
    return best;
  }

  /** All suppliers that match one tender, best first, one item per supplier. */
  function rankForNotice(buyer, notice, suppliers) {
    return suppliers
      .map((supplier) => bestOfSupplier(buyer, notice, supplier))
      .filter(Boolean)
      .sort((a, b) => b.score - a.score || (b.supplier.due_diligence?.total_score ?? 0) - (a.supplier.due_diligence?.total_score ?? 0));
  }

  /** The supplier's best item for one tender, or null. */
  function bestOfSupplier(buyer, notice, supplier) {
    let best = null;
    for (const item of supplier.catalogue?.items || []) {
      const score = scoreMatch(notice, item, supplier);
      if (score && (!best || score > best.score)) best = { score, buyer, supplier, notice, item };
    }
    return best;
  }

  /** Open tenders from all buyers that one supplier can answer, best first. */
  function opportunitiesFor(supplier, buyers) {
    return buyers
      .flatMap((buyer) => (buyer.recent_notices || []).map((notice) => bestOfSupplier(buyer, notice, supplier)))
      .filter(Boolean)
      .sort((a, b) => b.score - a.score || (b.notice.published > a.notice.published ? 1 : -1));
  }

  /** Pick a varied set of matches: different buyers first, then different suppliers. */
  function demoMatches(matches, count) {
    const picked = [];
    const buyers = new Set();
    const suppliers = new Set();
    for (const pass of [0, 1]) {
      for (const m of matches) {
        if (picked.length === count) return picked;
        if (picked.includes(m) || suppliers.has(m.supplier.id)) continue;
        if (pass === 0 && buyers.has(m.buyer.id)) continue;
        picked.push(m);
        buyers.add(m.buyer.id);
        suppliers.add(m.supplier.id);
      }
    }
    return picked;
  }

  /* ---------- formatting ---------- */
  const LEGAL_SUFFIX = /[,]?\s+(s\.r\.o\.|sp\. z o\.o\.|gmbh|ag|a\/s|ab|oy|oü|sia|uab|d\.o\.o\.|ltd\.?|limited|lda\.?|b\.v\.|s\.a\.|spa|s\.p\.a\.|slu|s\.l\.|sl|\(pty\) ltd|ood|storitve in trgovina)\b.*$/i;

  function shortName(name, max = 30) {
    let n = String(name || "").replace(/["“”„]/g, "").replace(LEGAL_SUFFIX, "").trim();
    if (n.length > max) n = n.slice(0, max - 1).trim() + "…";
    return n || name;
  }

  function fmtPrice(value, currency) {
    const digits = value < 1 ? 3 : value < 100 ? 2 : 0;
    return `${currency} ${value.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
  }

  const fmtInt = (n) => Math.round(n).toLocaleString("en-GB");

  function demoQuantity(item) {
    const unit = (item.unit || "").toLowerCase();
    if (/round|cartridge|shell|grenade/.test(unit)) return { qty: /grenade|shell/.test(unit) ? 2000 : 250000, unit: unit || "rounds" };
    if (/piece|rifle|weapon|unit|device/.test(unit)) return { qty: 40, unit: unit || "pieces" };
    return { qty: 100, unit: unit || "units" };
  }

  function tedSources(item, limit = 3) {
    return (item.evidence.sources || [])
      .filter((s) => s.notice_number)
      .slice(0, limit)
      .map((s) => ({ label: `TED ${s.notice_number}`, url: s.url || `https://ted.europa.eu/en/notice/-/detail/${s.notice_number}` }));
  }

  /* ---------- conversation scripts ---------- */
  function buildDeal({ buyer, supplier, notice, item }) {
    const buyerShort = shortName(buyer.name);
    const supplierShort = shortName(supplier.name);
    const anchor = parsePrice(item.commercial.derived_unit_price);
    const { qty, unit } = demoQuantity(item);
    const riskFlags = supplier.capabilities?.risk_flags || [];
    const dd = supplier.due_diligence;
    const tr = supplier.track_record || {};
    const itr = item.track_record || {};
    const refName = tr.repeat_buyers?.[0]?.name || supplier.contracts?.[0]?.buyer || "";
    const days = 90;
    const events = [];
    const say = (from, text, extra = {}) => events.push({ from, type: "text", text, ...extra });
    const card = (from, kind, data) => events.push({ from, type: "card", kind, data });
    const sys = (text) => events.push({ from: "system", type: "system", text });

    sys(`Agents connected · simulated run on real tender and profile data`);
    say("buyer", `Hello from the procurement agent of ${buyer.name}. We have an open requirement: “${notice.title}”.`, {
      // Requirements posted on the platform have no TED notice.
      evidence: notice.ted_url
        ? [{ label: `TED ${notice.notice_number}`, url: notice.ted_url }, { label: `published ${notice.published}` }]
        : [{ label: `posted on the platform ${notice.published}` }],
    });

    let mandate = null;
    if (anchor) {
      mandate = {
        qty, unit, currency: anchor.currency,
        max: anchor.value * 1.03,
        open: anchor.value * 1.08,
        counter: anchor.value * 0.96,
        final: anchor.value * 1.01,
        days, maxRounds: 3,
      };
      card("buyer", "mandate", {
        title: "My mandate for this run (demo values)",
        rows: [
          ["Quantity", `${fmtInt(qty)} ${unit}`],
          ["Max unit price", fmtPrice(mandate.max, anchor.currency)],
          ["Delivery", `within ${days} days`],
          ["Supplier evidence", "≥ document_verified"],
          ["Rounds", `max ${mandate.maxRounds}`],
        ],
        note: "Quantity and mandate are fictitious; the tender is real.",
      });
    }

    const brand = item.brand_model ? ` (${item.brand_model})` : "";
    say("supplier", `Hi, sales agent of ${supplier.name} here. We can supply: ${item.product}${brand}.`);
    if (item.evidence.verification_status === "document_verified") {
      const largest = itr.largest_order ? ` Largest order so far: ${itr.largest_order}.` : "";
      say("supplier", `Not just a claim: we were awarded this ${itr.deliveries || 1}× on TED, most recently ${itr.last_awarded || "recently"}.${largest}`, {
        evidence: [{ label: "document_verified", kind: "verified" }, ...tedSources(item)],
      });
    } else {
      say("supplier", `This line is in our declared catalogue; no TED award proves it yet, so treat it as a company claim.`, {
        evidence: [{ label: "declared", kind: "declared" }],
      });
    }

    say("buyer", "Thanks. Running KYC and deal due diligence before we talk price.");
    if (dd && typeof dd.total_score === "number") {
      card("system", riskFlags.length || dd.total_score < 6 ? "warn" : "info", {
        title: `Due diligence: ${dd.total_score.toFixed(1)}/10 · ${dd.verdict}`,
        rows: (dd.gates || []).map((g) => [g.check, g.status === "pass" ? "✔ pass" : g.status === "fail" ? "✖ fail" : g.status]),
        note: `Automated screening of free public data (${dd.assessed_at}), confidence ${dd.confidence}.`,
      });
    } else {
      card("system", riskFlags.length ? "warn" : "info", {
        title: "KYC: identity matched on TED",
        rows: [
          ["Lots won", `${tr.lots_won ?? "?"} since ${tr.first_award || "?"}`],
          ["Distinct buyers", String(tr.distinct_buyers ?? "?")],
          ["Competitive wins", `${tr.competitive_wins ?? 0} (sole bid: ${tr.sole_bid_wins ?? 0})`],
        ],
        note: "Full due-diligence score not computed yet for this supplier.",
      });
    }
    for (const flag of riskFlags) say("buyer", `Risk flag on file: ${flag.flag}.`, flag.source ? { evidence: [{ label: "source", url: flag.source }] } : {});

    let referee = null;
    if (refName) {
      referee = { name: refName, short: shortName(refName) };
      say("buyer", `Requesting a peer reference from the agent of ${refName}, a past customer of ${supplierShort} on TED.`);
      sys(`${referee.short} agent joined the conversation`);
      say("reference", `Reference for ${supplierShort}: on-time delivery: yes · quality issues: none · full quantity delivered: yes · would work with them again: yes.`, {
        evidence: [{ label: "seeded demo reference, not real feedback", kind: "inferred" }],
      });
      sys(`${referee.short} agent left the conversation`);
      say("buyer", "Reference stored as peer_attested. Please send your quote.");
    }

    if (!anchor) {
      say("supplier", "There is no public awarded unit price for this item, so I can't quote inside an automated run. Please send the full specification and quantities.");
      card("system", "info", {
        title: "RFQ issued",
        rows: [["Item", item.product], ["Next step", "Formal quote; human sign-off required"]],
        note: "Without a price anchor the agents stop at the RFQ stage instead of inventing a price.",
      });
      return { events, mandate, referee, outcome: "rfq" };
    }

    const cur = anchor.currency;
    card("supplier", "quote", {
      title: "Quote · round 1",
      rows: [
        ["Unit price", fmtPrice(mandate.open, cur)],
        ["Quantity", `${fmtInt(qty)} ${unit}`],
        ["Total", fmtPrice(mandate.open * qty, cur)],
        ["Delivery", item.commercial.delivery_terms || `${days} days`],
        ["Valid", "30 days"],
      ],
      note: `Price anchored to the unit price awarded in a real TED contract: ${item.commercial.derived_unit_price}`,
    });
    say("buyer", `${fmtPrice(mandate.open, cur)} is above my ceiling. Counter-offer: ${fmtPrice(mandate.counter, cur)} per unit, same delivery.`);
    say("supplier", `We can meet you at ${fmtPrice(mandate.final, cur)} if delivery is split into two batches.`);
    say("buyer", `${fmtPrice(mandate.final, cur)} is inside my mandate. Two batches accepted.`);

    const needsHuman = riskFlags.length > 0 || (dd && dd.total_score < 6);
    if (needsHuman) {
      card("system", "approval", {
        title: "Human sign-off required",
        rows: [["Why", riskFlags.length ? "supplier has open risk flags" : "due-diligence score below 6/10"], ["Mandate rule", "flagged suppliers need human approval"]],
        note: "The agents stay inside their mandate: they negotiated, but they may not sign.",
      });
    }
    card("system", "deal", {
      title: needsHuman ? "Deal closed after human approval" : "Deal closed autonomously inside mandate",
      rows: [
        ["Item", item.product],
        ["Quantity", `${fmtInt(qty)} ${unit}`],
        ["Unit price", fmtPrice(mandate.final, cur)],
        ["Total", fmtPrice(mandate.final * qty, cur)],
        ["Delivery", "2 batches, within 90 days"],
      ],
      audit: [
        `Price ${fmtPrice(mandate.final, cur)} ≤ mandate ${fmtPrice(mandate.max, cur)} ✔`,
        `Supplier evidence: ${item.evidence.verification_status} ✔`,
        referee ? "Peer reference recorded (seeded, peer_attested)" : "No peer reference available",
        dd ? `Due diligence: ${dd.verdict}` : "KYC: identity matched on TED",
        "Rounds used: 2 of 3",
      ],
      note: "Simulated deal. No order has been placed.",
    });
    sys("Transaction record written to the audit trail");
    return { events, mandate, referee, outcome: needsHuman ? "approval" : "deal" };
  }

  /** Fallback when a company has no catalogue match: matchmaking agent intro. */
  function buildIntro(company) {
    const events = [];
    const isSupplier = company.role === "supply";
    events.push({ from: "system", type: "system", text: "Matchmaking agent connected" });
    if (isSupplier) {
      events.push({ from: "buyer", type: "text", text: `Hello ${shortName(company.name)}. I'm the platform's matchmaking agent. What can you deliver with proof?` });
      const items = (company.catalogue?.items || []).filter((i) => i.evidence.verification_status === "document_verified").slice(0, 3);
      for (const item of items) {
        events.push({ from: "supplier", type: "text", text: `${item.product}: awarded ${item.track_record?.deliveries || 1}× on TED, last ${item.track_record?.last_awarded || "n/a"}.`, evidence: [{ label: "document_verified", kind: "verified" }, ...tedSources(item, 2)] });
      }
      events.push({ from: "buyer", type: "text", text: "Added to your watchlist. I'll start a negotiation when a matching tender opens." });
    } else {
      const notice = (company.recent_notices || [])[0];
      // In intros the company sits on the left ("supplier" side), the matchmaking agent on the right.
      if (notice) events.push({ from: "supplier", type: "text", text: `Procurement agent of ${shortName(company.name)} here. Our latest requirement: “${notice.title}”.`, evidence: [{ label: `TED ${notice.notice_number}`, url: notice.ted_url }] });
      events.push({ from: "buyer", type: "text", text: "No supplier on the platform has a proven match for this yet. I'll open a call for suppliers and notify you." });
    }
    return { events, mandate: null, referee: null, outcome: "intro" };
  }

  window.Agents = { calibreKeys, findMatches, bestForNotice, rankForNotice, opportunitiesFor, demoMatches, buildDeal, buildIntro, shortName, parsePrice, fmtPrice };
})();
