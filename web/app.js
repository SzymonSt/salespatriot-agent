/* SalesPatriot local UI: overview, supplier and buyer lists and pages, floating agent chats.
 * Clean URLs (/supplier/<id>, /buyer/<id>) need serve.py, which answers them with index.html. */
(function () {
  "use strict";

  const A = window.Agents;
  const DATA_URL = "/data/profiles/";
  const MAX_OPEN = () => (window.innerWidth < 760 ? 1 : window.innerWidth < 1240 ? 2 : 3);
  const COUNTRIES = {
    AUT: "Austria", BEL: "Belgium", BGR: "Bulgaria", HRV: "Croatia", CYP: "Cyprus", CZE: "Czechia",
    DNK: "Denmark", EST: "Estonia", FIN: "Finland", FRA: "France", DEU: "Germany", GRC: "Greece",
    HUN: "Hungary", IRL: "Ireland", ITA: "Italy", LVA: "Latvia", LTU: "Lithuania", LUX: "Luxembourg",
    MLT: "Malta", NLD: "Netherlands", NOR: "Norway", POL: "Poland", PRT: "Portugal", ROU: "Romania",
    SVK: "Slovakia", SVN: "Slovenia", ESP: "Spain", SWE: "Sweden", GBR: "United Kingdom",
    ZAF: "South Africa", CHE: "Switzerland",
  };
  const EVIDENCE = {
    document_verified: ["Verified on TED", "verified"],
    declared: ["Declared", "neutral"],
    inferred: ["Example", "neutral"],
  };
  const PREVIEW = 6;

  const state = { suppliers: [], buyers: [], byId: new Map(), logos: {}, matches: [], demo: [], convs: new Map(), open: [],
    sessionId: null, liveViews: [], requests: {}, requestsSeeded: new Set() };

  /* ---------- DOM helpers ---------- */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const country = (iso3) => COUNTRIES[iso3] || iso3 || "";
  const trim = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s || "");
  const status = (text, tone) => h("span", { class: `status ${tone}` }, text);
  const external = (href, text) => h("a", { href, target: "_blank", rel: "noopener" }, text);
  const money = (value, currency) => A.fmtPrice(value, currency);
  function initials(name) {
    const words = A.shortName(name, 60).replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean);
    return ((words[0]?.[0] || "?") + (words[1]?.[0] || "")).toUpperCase();
  }
  function avatar(entity, size = "") {
    const logo = state.logos[entity.id];
    const fallback = () => entity.initials || initials(entity.name);
    if (!logo) return h("span", { class: `avatar ${size}`, "aria-hidden": "true" }, fallback());
    const el = h("span", { class: `avatar logo ${size}`, "aria-hidden": "true", style: logo.background ? `background:${logo.background}` : null });
    el.append(h("img", { src: `/logos/${logo.file}`, alt: "", loading: "lazy",
      onerror: () => { el.classList.remove("logo"); el.removeAttribute("style"); el.replaceChildren(fallback()); } }));
    return el;
  }

  /* ---------- URLs ---------- */
  // /supplier/pol-9482182612 ↔ profile id supplier-pol-9482182612
  const pathFor = (c) => `/${c.role === "demand" ? "buyer" : "supplier"}/${encodeURIComponent(c.id.replace(/^(supplier|buyer)-/, ""))}`;
  const link = (href, props, ...children) => h("a", { href, ...props }, ...children);
  function navigate(href) {
    history.pushState({}, "", href);
    route();
  }

  /* ---------- data ---------- */
  async function load() {
    const [suppliers, buyers] = await Promise.all(["suppliers.json", "buyers.json"].map((f) =>
      fetch(DATA_URL + f).then((r) => { if (!r.ok) throw new Error(`${f}: HTTP ${r.status}`); return r.json(); })));
    state.suppliers = suppliers.sort((a, b) => a.name.localeCompare(b.name));
    state.buyers = buyers.sort((a, b) => a.name.localeCompare(b.name));
    for (const c of [...suppliers, ...buyers]) state.byId.set(c.id, c);
    // Logos are optional: without the index every company shows initials.
    state.logos = await fetch("/logos/logos.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    state.matches = A.findMatches(buyers, suppliers);
    state.demo = A.demoMatches(state.matches, 6);
  }

  /* ---------- routing ---------- */
  function route() {
    const [, section = "", id = ""] = location.pathname.split("/");
    const isCompany = section === "supplier" || section === "buyer";
    const company = isCompany && id ? state.byId.get(`${section}-${decodeURIComponent(id)}`) : null;
    const page = isCompany ? `${section}s` : section === "signin" ? "workspace" : section;
    state.liveViews = [];
    document.querySelectorAll(".nav nav a").forEach((a) => {
      if (a.dataset.route === page) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    let view;
    if (company) view = section === "supplier" ? supplierPage(company) : buyerPage(company);
    else if (isCompany) view = notFound(section);
    else if (section === "signin") view = signinPage();
    else if (section === "workspace") view = sessionCompany() ? workspace(sessionCompany()) : signinPage();
    else if (section === "suppliers") view = listPage("supply");
    else if (section === "buyers") view = listPage("demand");
    else view = overview();
    document.getElementById("main").replaceChildren(view);
    document.title = company ? `${company.name} · SalesPatriot` : "SalesPatriot";
    window.scrollTo(0, 0);
  }

  const notFound = (section) => h("div", { class: "page" },
    h("h1", { class: "title" }, `No such ${section}`),
    h("p", { class: "secondary" }, "The link may be out of date. ", link(`/${section}s`, {}, `Browse ${section}s ›`)));

  /* ---------- overview ---------- */
  function overview() {
    const verified = state.suppliers.reduce((n, s) => n + (s.catalogue?.item_counts?.document_verified || 0), 0);
    const countries = new Set([...state.suppliers, ...state.buyers].map((c) => c.country)).size;
    return h("div", { class: "page" },
      h("section", { class: "hero" },
        h("h1", {}, "Agents that close", h("br"), "defence deals."),
        h("p", {}, "Every EU buyer and supplier is represented by an agent. Agents match real tenders to proven capability, check references with each other and close inside the limits their people set."),
        h("div", { class: "hero-actions" },
          h("button", { class: "btn", type: "button", onclick: runDemo }, "Watch a negotiation"),
          link("/suppliers", { class: "btn btn-quiet" }, "Browse suppliers ›"))),
      transcript(state.demo[0]),
      h("section", { class: "points", "aria-label": "How it works" },
        point("Proven, not claimed", "Supplier agents answer with products they have delivered under real TED contracts."),
        point("Checked by peers", "Before talking price, buyer agents ask the agents of past customers for a reference."),
        point("Bound by mandate", "Agents close only inside limits their people set. Anything else goes to a person.")),
      h("p", { class: "facts" },
        h("b", {}, state.suppliers.length), " suppliers · ", h("b", {}, state.buyers.length), " buyers · ",
        h("b", {}, verified), " products verified on TED · ", h("b", {}, countries), " countries"));
  }
  const point = (title, text) => h("div", {}, h("h2", {}, title), h("p", {}, text));

  function transcript(m) {
    if (!m) return null;
    const script = A.buildDeal(m);
    const buyer = A.shortName(m.buyer.name, 40);
    const supplier = A.shortName(m.supplier.name, 40);
    const line = (who, text, right) => h("div", { class: `line ${right ? "right" : ""}` }, h("span", { class: "caption" }, who), h("div", { class: "bubble" }, text));
    const done = script.mandate ? `${money(script.mandate.final, script.mandate.currency)} per unit is inside my mandate. Deal.` : "Request for quote sent.";
    return h("section", { class: "transcript", "aria-label": "Example negotiation" },
      h("div", { class: "transcript-head" },
        h("span", { class: "transcript-who" }, avatar(m.buyer, "sm"), avatar(m.supplier, "sm"), h("b", {}, `${buyer} and ${supplier}`)),
        h("span", { class: "caption" }, "Simulated on real TED data")),
      line(`${buyer} · buyer agent`, `We need “${trim(m.notice.title, 110)}”.`, true),
      line(`${supplier} · supplier agent`, `${m.item.product}. Delivered ${m.item.track_record?.deliveries || 1}× under TED contracts, most recently ${m.item.track_record?.last_awarded || "this year"}.`),
      script.referee ? line(`${A.shortName(script.referee.name, 40)} · past customer (demo reference)`, "Delivered on time and in full. We would work with them again.") : null,
      line(`${buyer} · buyer agent`, done, true));
  }

  /* ---------- list pages ---------- */
  function listPage(role) {
    const isSupply = role === "supply";
    const all = isSupply ? state.suppliers : state.buyers;
    const list = h("ul", { class: "list" });
    const search = h("input", { class: "field", type: "search", placeholder: isSupply ? "Search products or companies" : "Search tenders or buyers", "aria-label": "Search" });
    const countries = [...new Set(all.map((c) => c.country))].sort((a, b) => country(a).localeCompare(country(b)));
    const select = h("select", { class: "field", "aria-label": "Country" },
      h("option", { value: "" }, "All countries"), countries.map((c) => h("option", { value: c }, country(c))));
    const sort = isSupply ? h("select", { class: "field", "aria-label": "Sort" },
      h("option", { value: "name" }, "Sort by name"), h("option", { value: "dd" }, "Sort by due diligence")) : null;
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = all.filter((c) => (!select.value || c.country === select.value) && (!q || searchText(c).includes(q)));
      if (sort?.value === "dd") shown.sort((a, b) => (b.due_diligence?.total_score ?? -1) - (a.due_diligence?.total_score ?? -1));
      list.replaceChildren(...(shown.length ? shown.map(listRow) : [h("li", { class: "empty" }, "No matches. Try another search or country.")]));
    };
    search.addEventListener("input", draw);
    select.addEventListener("change", draw);
    sort?.addEventListener("change", draw);
    draw();
    return h("div", { class: "page" },
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, isSupply ? "Suppliers" : "Buyers"),
          h("p", {}, isSupply ? "Companies with weapons and ammunition contracts on TED, and what they have proven they can deliver."
            : "Defence and security buyers with recent weapons and ammunition tenders.")),
        h("div", { class: "filters" }, search, select, sort)),
      list);
  }

  function searchText(c) {
    return (c._search ||= [c.name, c.legal_name, c.city, country(c.country), c.summary,
      ...(c.catalogue?.items || []).map((i) => i.product), ...(c.recent_notices || []).map((n) => n.title)].join(" ").toLowerCase());
  }

  // "Limited evidence" is not a warning: it means free open data was thin.
  const verdictTone = (v) => (/strong|good/i.test(v) ? "verified" : /limited/i.test(v) ? "neutral" : /moderate|elevated|review/i.test(v) ? "caution" : "risk");
  const businessModel = (c) => (c.offering?.business_model && c.offering.business_model !== "unknown" ? c.offering.business_model.replace(/_/g, " ") : "");

  function listRow(c) {
    const flags = c.capabilities?.risk_flags || [];
    const sub = c.role === "supply"
      ? [country(c.country), businessModel(c), `${c.catalogue?.item_counts?.document_verified || 0} products verified on TED`].filter(Boolean).join(" · ")
      : `${country(c.country)} · ${c.ted_activity?.notices_total ?? 0} notices on TED`;
    return h("li", {}, link(pathFor(c), { class: "row-head" },
      avatar(c),
      h("span", { style: "min-width:0" }, h("div", { class: "row-title" }, c.name), h("div", { class: "row-sub" }, sub)),
      h("span", { class: "row-side" },
        c.due_diligence ? h("span", { class: "grade" }, h("b", {}, c.due_diligence.total_score.toFixed(1)), status(c.due_diligence.verdict, verdictTone(c.due_diligence.verdict))) : null,
        flags.length ? status(flags.length > 1 ? `${flags.length} risk flags` : "Risk flag", "risk") : null),
      h("span", { class: "chevron", "aria-hidden": "true" }, "›")));
  }

  /* ---------- company pages ---------- */
  function pageHeader(c, back, meta) {
    return h("header", { class: "company-head" },
      link(back.href, { class: "back" }, `‹ ${back.label}`),
      h("div", { class: "company-id" },
        avatar(c, "lg"),
        h("div", { style: "min-width:0" },
          h("h1", {}, c.name),
          c.legal_name && c.legal_name !== c.name ? h("p", { class: "secondary" }, c.legal_name) : null,
          h("p", { class: "secondary" }, meta.filter(Boolean).join(" · ")))),
      h("div", { class: "company-actions" },
        h("button", { class: "btn", type: "button", onclick: () => openFor(c) }, "Message agent"),
        c.website ? h("a", { class: "btn btn-quiet", href: c.website, target: "_blank", rel: "noopener" }, "Website ›") : null,
        sessionCompany()?.id === c.id ? link("/workspace", { class: "btn btn-quiet" }, "Your workspace ›")
          : h("button", { class: "btn btn-quiet", type: "button", onclick: () => { setSession(c.id); navigate("/workspace"); } }, "Sign in as this company")));
  }

  const section = (title, ...body) => h("section", { class: "section" }, h("h2", {}, title), ...body);
  const group = (...children) => h("div", { class: "group" }, ...children);
  const stat = (value, label) => h("div", { class: "stat" }, h("b", {}, value), h("span", {}, label));

  /** A list that shows the first PREVIEW rows and a "Show all" button for the rest. */
  function expandable(items, render) {
    const ul = h("ul", { class: "rows" });
    let all = items.length <= PREVIEW;
    const more = h("button", { class: "btn btn-quiet show-all", type: "button" });
    const draw = () => {
      ul.replaceChildren(...(all ? items : items.slice(0, PREVIEW)).map(render));
      more.hidden = all;
      more.textContent = `Show all ${items.length}`;
    };
    more.addEventListener("click", () => { all = true; draw(); });
    draw();
    return group(ul, more);
  }

  function supplierPage(s) {
    const t = s.track_record || {};
    const cap = s.capabilities || {};
    const flags = cap.risk_flags || [];
    const dd = s.due_diligence;
    const items = s.catalogue?.items || [];
    return h("div", { class: "page" },
      pageHeader(s, { href: "/suppliers", label: "Suppliers" }, [country(s.country), s.city, businessModel(s), s.company_size && `${s.company_size} company`]),
      flags.length ? h("div", { class: "notice risk" }, h("b", {}, flags.length > 1 ? "Risk flags" : "Risk flag"),
        h("ul", {}, flags.map((f) => h("li", {}, f.flag, f.source ? [" ", external(f.source, "Source")] : null)))) : null,
      h("p", { class: "lead" }, s.catalogue?.summary || s.summary),
      h("div", { class: "stats" },
        stat(t.lots_won ?? 0, "lots won on TED"),
        stat(t.distinct_buyers ?? 0, "buyers"),
        stat(Object.keys(t.buyer_countries || {}).length, "buyer countries"),
        stat(s.catalogue?.item_counts?.document_verified || 0, "products verified"),
        dd ? stat(dd.total_score.toFixed(1), "due diligence / 10") : null),
      section("What they can provide",
        group(h("div", { class: "split" },
          h("div", {}, h("h3", {}, "Proven on TED contracts"), h("ul", { class: "bullets" }, (cap.proven || []).map((x) => h("li", {}, x)))),
          h("div", {}, h("h3", {}, "Offered by the company"), h("ul", { class: "bullets" }, (cap.offered || []).map((x) => h("li", {}, x))))))),
      section(`Products (${items.length})`, expandable(items, productRow)),
      dd ? section("Due diligence", dueDiligence(dd)) : null,
      section(`Contract awards (${s.contracts?.length || 0})`, expandable(s.contracts || [], contractRow)),
      s.offering ? section("About the company", about(s.offering)) : null);
  }

  function productRow(i) {
    const [label, tone] = EVIDENCE[i.evidence.verification_status] || ["", "neutral"];
    const specs = Object.entries(i.specs || {}).filter(([, v]) => String(v).length < 60).slice(0, 3).map(([, v]) => v);
    const tr = i.track_record || {};
    return h("li", { class: "row" },
      h("div", { class: "row-main" },
        h("div", { class: "row-title wrap" }, i.product),
        h("div", { class: "row-sub wrap" }, [i.brand_model, ...specs].filter(Boolean).join(" · ")),
        tr.largest_order ? h("div", { class: "row-sub wrap" }, `Largest order: ${tr.largest_order}`) : null,
        i.commercial?.derived_unit_price ? h("div", { class: "row-sub wrap" }, `Awarded price: ${i.commercial.derived_unit_price}`) : null),
      h("div", { class: "row-end" }, status(label, tone),
        tr.deliveries ? h("span", { class: "caption" }, `${tr.deliveries} award${tr.deliveries > 1 ? "s" : ""}`) : null));
  }

  function contractRow(k) {
    const v = k.awarded_value && k.awarded_value.value > 0 ? money(k.awarded_value.value, k.awarded_value.currency) : null;
    return h("li", { class: "row" },
      h("div", { class: "row-main" },
        h("div", { class: "row-title wrap" }, k.title && k.title.length > 14 ? k.title : k.notice_title),
        h("div", { class: "row-sub wrap" }, [k.buyer, country(k.buyer_country), k.published,
          k.tenders_received ? `${k.tenders_received} bid${k.tenders_received > 1 ? "s" : ""}` : null,
          k.framework_agreement ? "framework" : null].filter(Boolean).join(" · "))),
      h("div", { class: "row-end" }, v ? h("span", {}, v) : null, external(k.ted_url, "TED ›")));
  }

  function dueDiligence(dd) {
    const tone = verdictTone(dd.verdict);
    const cats = Object.values(dd.categories || {});
    return group(
      h("div", { class: "dd-head" },
        h("b", { class: "dd-score" }, dd.total_score.toFixed(1)),
        h("div", {}, status(dd.verdict, tone), h("div", { class: "caption" }, `Confidence ${dd.confidence} · assessed ${dd.assessed_at}`))),
      h("ul", { class: "rows" }, cats.map((c) => h("li", { class: "row" },
        h("div", { class: "row-main" }, h("div", { class: "row-title" }, c.label), h("div", { class: "row-sub wrap" }, c.reasoning)),
        h("div", { class: "row-end" }, typeof c.score === "number" ? h("b", {}, c.score.toFixed(1)) : h("span", { class: "caption" }, "No data"))))),
      (dd.red_flags || []).length ? h("div", { class: "notice risk inset" }, h("b", {}, "Red flags"), h("ul", {}, dd.red_flags.map((f) => h("li", {}, f)))) : null,
      dd.disclaimer ? h("p", { class: "caption pad" }, dd.disclaimer) : null);
  }

  function about(o) {
    return group(h("div", { class: "pad stack" },
      o.about ? h("p", {}, o.about) : null,
      o.certifications?.length ? h("div", {}, h("h3", {}, "Certifications and licences"), h("ul", { class: "bullets" }, o.certifications.map((x) => h("li", {}, x)))) : null,
      o.brands_represented?.length ? h("div", {}, h("h3", {}, "Brands represented"), h("p", { class: "secondary" }, o.brands_represented.slice(0, 40).join(" · "))) : null,
      o.production_capabilities ? h("div", {}, h("h3", {}, "Production"), h("p", {}, o.production_capabilities)) : null,
      o.current_offers?.length ? h("div", {}, h("h3", {}, "Recent offers and news"), h("ul", { class: "bullets" }, o.current_offers.map((x) =>
        h("li", {}, h("b", {}, x.item), ` — ${x.description} `, x.source ? external(x.source, "Source") : null)))) : null,
      h("p", { class: "caption" }, `Researched ${o.researched_at || ""} from public sources; confidence ${o.confidence}.`)));
  }

  function buyerPage(b) {
    const notices = b.recent_notices || [];
    const noticeMatch = new Map(notices.map((n) => [n.notice_number, A.bestForNotice(b, n, state.suppliers)]));
    const matched = [...noticeMatch.values()].filter(Boolean);
    const bySupplierName = new Map(state.suppliers.flatMap((s) => [[s.name, s], [s.legal_name, s]]));
    return h("div", { class: "page" },
      pageHeader(b, { href: "/buyers", label: "Buyers" }, [country(b.country), b.city, b.main_activity && `main activity: ${b.main_activity}`]),
      h("p", { class: "lead" }, b.summary),
      h("div", { class: "stats" },
        stat(b.ted_activity?.notices_total ?? 0, "notices on TED"),
        stat(notices.length, "recent weapons and ammunition tenders"),
        stat(matched.length, "tenders with a supplier match"),
        stat(new Set(matched.map((m) => m.supplier.id)).size, "matching suppliers")),
      section(`Recent weapons and ammunition tenders (${notices.length})`, expandable(notices, (n) => {
        const match = noticeMatch.get(n.notice_number);
        return h("li", { class: "row" },
          h("div", { class: "row-main" },
            h("div", { class: "row-title wrap" }, n.title),
            h("div", { class: "row-sub wrap" }, [n.published, n.value ? money(Number(n.value), n.currency || "EUR") : null].filter(Boolean).join(" · "), " · ", external(n.ted_url, "TED ›"))),
          h("div", { class: "row-end" }, match
            ? h("button", { class: "btn btn-quiet", type: "button", onclick: () => openMatch(match) }, `Match: ${A.shortName(match.supplier.name, 22)}`)
            : status("No match yet", "neutral")));
      })),
      section("What they buy most", group(h("ul", { class: "rows" }, (b.top_cpv || []).slice(0, 6).map((c) =>
        h("li", { class: "row" }, h("div", { class: "row-main" }, h("div", { class: "row-title" }, c.label || c.code)),
          h("div", { class: "row-end" }, h("span", { class: "caption" }, `${c.notices} notices`))))))),
      section("Suppliers they work with", group(h("ul", { class: "rows" }, (b.top_counterparts || []).slice(0, 8).map((c) => {
        const known = bySupplierName.get(c.name);
        return h("li", { class: "row" },
          h("div", { class: "row-main" }, h("div", { class: "row-title" }, known ? link(pathFor(known), {}, c.name) : c.name)),
          h("div", { class: "row-end" }, h("span", { class: "caption" }, `${c.notices} notice${c.notices > 1 ? "s" : ""}`)));
      })))));
  }


  /* ---------- session (demo sign-in: no password, remembered in this browser) ---------- */
  const SESSION_KEY = "salespatriot.session";
  const sessionCompany = () => state.byId.get(state.sessionId) || null;
  function restoreSession() {
    try { state.sessionId = localStorage.getItem(SESSION_KEY); } catch { /* storage blocked: start signed out */ }
  }
  function setSession(id) {
    state.sessionId = id;
    try { id ? localStorage.setItem(SESSION_KEY, id) : localStorage.removeItem(SESSION_KEY); } catch { /* storage blocked: lasts until reload */ }
    renderSession();
  }
  function renderSession() {
    const el = document.getElementById("session-link");
    const c = sessionCompany();
    el.href = c ? "/workspace" : "/signin";
    el.replaceChildren(...(c ? [avatar(c, "xs"), h("span", {}, A.shortName(c.name, 22))] : ["Sign in"]));
  }
  // Sections that show conversation state redraw when a conversation changes.
  const refreshLive = () => state.liveViews.forEach((draw) => draw());

  function signinPage() {
    const pick = (c) => h("li", {}, h("button", { class: "row-head", type: "button", onclick: () => { setSession(c.id); navigate("/workspace"); } },
      avatar(c),
      h("span", { style: "min-width:0" }, h("div", { class: "row-title" }, c.name), h("div", { class: "row-sub" }, country(c.country))),
      h("span", { class: "row-side" }), h("span", { class: "chevron", "aria-hidden": "true" }, "›")));
    return h("div", { class: "page narrow" },
      h("h1", { class: "title" }, "Sign in"),
      h("p", { class: "secondary intro" }, "Demo sign-in: pick the company whose agent workspace you want to open. There is no password, and nothing leaves this browser."),
      section("Suppliers", h("ul", { class: "list" }, state.suppliers.map(pick))),
      section("Buyers", h("ul", { class: "list" }, state.buyers.map(pick))));
  }

  function workspace(c) {
    return c.role === "supply" ? supplierWorkspace(c) : buyerWorkspace(c);
  }

  function workspaceHeader(c, agentJob) {
    return h("header", { class: "company-head" },
      h("p", { class: "eyebrow" }, "Signed in as"),
      h("div", { class: "company-id" },
        avatar(c, "lg"),
        h("div", { style: "min-width:0" },
          h("h1", {}, c.name),
          h("p", { class: "secondary" }, status("Agent active", "verified"), ` · ${agentJob}`))),
      h("div", { class: "company-actions" },
        link(pathFor(c), { class: "btn btn-quiet" }, "Public profile ›"),
        h("button", { class: "btn btn-quiet", type: "button", onclick: () => { setSession(null); navigate("/"); } }, "Sign out")));
  }

  const rowsGroup = (items, empty) => group(h("ul", { class: "rows" }, items.length ? items : [h("li", { class: "row" }, h("div", { class: "row-main secondary" }, empty))]));
  const row = (title, sub, end) => h("li", { class: "row" },
    h("div", { class: "row-main" }, h("div", { class: "row-title wrap" }, title), sub ? h("div", { class: "row-sub wrap" }, sub) : null),
    h("div", { class: "row-end" }, end));
  const mandate = (rows) => group(h("dl", { class: "mandate" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
    h("p", { class: "caption pad" }, "Demo mandate. In production your team sets these limits when signing the agent mandate (KYC check K11)."));

  function kycItems(c) {
    const labels = { needs_upload: ["Upload needed", "caution"], pending_platform: ["Pending", "neutral"], fail: ["Failed", "risk"] };
    return Object.entries(c.kyc?.checks || {}).filter(([, k]) => labels[k.status])
      .map(([code, k]) => row(`${code} · ${k.title}`, k.summary, status(...labels[k.status])));
  }

  function liveSection(title, buildRows, empty) {
    const holder = h("div", {});
    const draw = () => holder.replaceChildren(rowsGroup(buildRows(), empty));
    draw();
    state.liveViews.push(draw);
    return section(title, holder);
  }

  function conversationRows(c) {
    return [...state.convs.values()]
      .filter((v) => v.people.buyer.id === c.id || v.people.supplier.id === c.id)
      .map((v) => {
        const other = v.people.buyer.id === c.id ? v.people.supplier : v.people.buyer;
        return row(other.name, v.subtitle.textContent, h("button", { class: "btn btn-quiet", type: "button", onclick: () => showConv(v.id) }, "Open"));
      });
  }

  function supplierWorkspace(s) {
    const opps = A.opportunitiesFor(s, state.buyers);
    const counts = s.catalogue?.item_counts || {};
    const dd = s.due_diligence;
    const categories = [...new Set((s.catalogue?.items || []).filter((i) => i.evidence.verification_status === "document_verified").map((i) => i.category))];
    return h("div", { class: "page" },
      workspaceHeader(s, `your agent answers tenders that match your ${counts.document_verified || 0} TED-verified products`),
      h("div", { class: "stats" },
        stat(opps.length, "matching open tenders"),
        stat(counts.document_verified || 0, "products verified on TED"),
        stat(counts.declared || 0, "declared, not yet proven"),
        dd ? stat(dd.total_score.toFixed(1), `due diligence · ${dd.verdict}`) : null),
      liveSection("Needs your attention", () => [
        ...(s.capabilities?.risk_flags || []).map((f) => row(`Risk flag on your profile: ${f.flag}`, "Buyers' agents see this and send deals to a person for approval. You can respond with documents.", status("Visible to buyers", "risk"))),
        ...kycItems(s),
      ], "Nothing right now."),
      liveSection("Requests from buyers", () => supplierRequestRows(s),
        "No new requests. When a buyer posts a requirement that matches your products, it pops up here with your deal check."),
      section(`Opportunities your agent found (${opps.length})`, opps.length ? expandable(opps, (m) => row(
        m.notice.title,
        [`${m.buyer.name} · ${country(m.buyer.country)} · ${m.notice.published} · `, external(m.notice.ted_url, "TED ›"),
          h("br"), `Your match: ${m.item.product} `, status(EVIDENCE[m.item.evidence.verification_status][0], EVIDENCE[m.item.evidence.verification_status][1])],
        h("button", { class: "btn btn-quiet", type: "button", onclick: () => openMatch(m) },
          m.item.evidence.verification_status === "document_verified" ? "Let agent respond" : "Respond (needs you)"))) : rowsGroup([], "No open tender matches your proven products yet. Your agent keeps watching new TED notices.")),
      liveSection("Conversations", () => conversationRows(s), "No conversations yet. Your agent starts one when it responds to a tender."),
      section("Your agent's mandate", mandate([
        ["Sells", categories.join(", ") || "Nothing proven yet"],
        ["Answers automatically", "Tenders that match a TED-verified product"],
        ["Price", "From the awarded TED unit price; never more than 3% below it"],
        ["Escalates to you", "Orders above EUR 1,000,000, products not yet verified, buyers outside the EU and NATO"],
        ["Mandate signed", s.kyc?.checks?.K11?.status === "pass" ? "Yes" : "Pending on the platform"],
      ])));
  }

  function buyerWorkspace(b) {
    const notices = b.recent_notices || [];
    const ranked = new Map(notices.map((n) => [n.notice_number, A.rankForNotice(b, n, state.suppliers)]));
    const bySupplier = new Map();
    for (const list of ranked.values()) for (const m of list) {
      const entry = bySupplier.get(m.supplier.id) || { supplier: m.supplier, best: m, tenders: 0 };
      entry.tenders += 1;
      bySupplier.set(m.supplier.id, entry);
    }
    const recommended = [...bySupplier.values()].sort((x, y) => y.tenders - x.tenders || (y.supplier.due_diligence?.total_score ?? 0) - (x.supplier.due_diligence?.total_score ?? 0));
    const buys = (b.top_cpv || []).filter((c) => c.code.startsWith("353")).map((c) => c.label).slice(0, 3);
    return h("div", { class: "page" },
      workspaceHeader(b, "your agent sources suppliers for your tenders and negotiates inside your mandate"),
      h("div", { class: "stats" },
        stat(notices.length, "open tenders"),
        stat([...ranked.values()].filter((l) => l.length).length, "with a proven supplier"),
        stat(recommended.length, "suppliers to consider")),
      liveSection("Needs your attention", () => [...state.convs.values()]
        .filter((v) => v.people.buyer.id === b.id && v.waiting)
        .map((v) => row(`Approve deal with ${v.people.supplier.name}`, `${v.topic}. Your mandate needs a person for suppliers with risk flags or low due diligence.`,
          h("button", { class: "btn", type: "button", onclick: () => showConv(v.id) }, "Review"))), "Nothing to approve right now."),
      section("Post a requirement", requirementForm(b)),
      liveSection("Your posted requests", () => buyerRequestRows(b), "Nothing posted yet. Post a requirement above; matched suppliers see it immediately."),
      section(`Your open tenders (${notices.length})`, expandable(notices, (n) => {
        const best = ranked.get(n.notice_number)[0];
        return row(n.title, [`${n.published} · `, external(n.ted_url, "TED ›"),
          best ? [h("br"), `Best proven supplier: ${best.supplier.name} (${best.item.product})`] : null],
          best ? h("button", { class: "btn btn-quiet", type: "button", onclick: () => openMatch(best) }, "Negotiate") : status("No proven supplier", "neutral"));
      })),
      section("Suppliers to consider", recommended.length ? expandable(recommended, (r) => {
        const dd = r.supplier.due_diligence;
        return h("li", { class: "row" },
          h("div", { class: "row-main row-with-avatar" }, avatar(r.supplier, "sm"),
            h("div", { style: "min-width:0" },
              h("div", { class: "row-title wrap" }, link(pathFor(r.supplier), {}, r.supplier.name)),
              h("div", { class: "row-sub wrap" }, `${country(r.supplier.country)} · proven for ${r.tenders} of your tenders`, dd ? [" · ", status(`${dd.total_score.toFixed(1)} ${dd.verdict}`, verdictTone(dd.verdict))] : null))),
          h("div", { class: "row-end" }, h("button", { class: "btn btn-quiet", type: "button", onclick: () => openMatch(r.best) }, "Negotiate")));
      }) : rowsGroup([], "No supplier has proven a match yet.")),
      liveSection("Conversations", () => conversationRows(b), "No conversations yet. Start one from a tender or a supplier."),
      section("Your agent's mandate", mandate([
        ["Buys", buys.join(", ") || "Weapons and ammunition"],
        ["Max unit price", "Up to 3% above the best awarded TED price for the item"],
        ["Supplier evidence", "TED-verified products only"],
        ["Negotiation", "At most 3 rounds"],
        ["Needs your approval", "Suppliers with risk flags or due diligence below 6/10"],
      ])));
  }

  function requirementForm(b) {
    const text = h("input", { class: "field grow", type: "text", required: true, placeholder: "e.g. 9×19 mm FMJ pistol cartridges", "aria-label": "What do you need?" });
    const cpv = h("select", { class: "field", "aria-label": "Category" },
      [["35330000", "Ammunition"], ["35320000", "Firearms"], ["35340000", "Parts of firearms and ammunition"], ["35310000", "Miscellaneous weapons"]]
        .map(([v, label]) => h("option", { value: v }, label)));
    const qty = h("input", { class: "field small", type: "number", min: "1", step: "1", placeholder: "Quantity", "aria-label": "Quantity" });
    const unit = h("select", { class: "field small", "aria-label": "Unit" }, ["rounds", "pieces", "sets"].map((u) => h("option", { value: u }, u)));
    const budget = h("input", { class: "field small", type: "number", min: "0", step: "1000", placeholder: "Budget EUR", "aria-label": "Budget in EUR" });
    const needBy = h("input", { class: "field small", type: "date", "aria-label": "Needed by",
      value: new Date(Date.now() + 120 * 864e5).toISOString().slice(0, 10) });
    const sent = h("p", { class: "caption pad", role: "status" });
    const results = h("div", {});
    const form = h("form", { class: "pad form", onsubmit: (e) => {
      e.preventDefault();
      const notice = { notice_number: `REQ-${Date.now()}`, title: text.value.trim(), description: "", cpv_codes: [cpv.value],
        published: new Date().toISOString().slice(0, 10), ted_url: "" };
      const list = A.rankForNotice(b, notice, state.suppliers).slice(0, 5);
      postRequest(b, notice, list, { quantity: Number(qty.value) || null, unit: unit.value, budget: Number(budget.value) || null, need_by: needBy.value }, sent);
      results.replaceChildren(h("ul", { class: "rows" }, list.length ? list.map((m) => {
        const dd = m.supplier.due_diligence;
        return row([link(pathFor(m.supplier), {}, m.supplier.name)],
          [m.item.product, " · ", status(EVIDENCE[m.item.evidence.verification_status][0], EVIDENCE[m.item.evidence.verification_status][1]),
            dd ? [" · ", status(`DD ${dd.total_score.toFixed(1)}`, verdictTone(dd.verdict))] : null],
          h("button", { class: "btn btn-quiet", type: "button", onclick: () => openMatch(m) }, "Negotiate"));
      }) : [h("li", { class: "row" }, h("div", { class: "row-main secondary" },
        "No supplier has proven this yet. Include the calibre (for example 5.56×45 mm) so your agent can match it."))]));
    } },
      h("label", { class: "caption" }, "Describe what you need. Your agent ranks suppliers that have delivered it under TED contracts."),
      h("div", { class: "form-row" }, text, cpv),
      h("div", { class: "form-row" }, qty, unit, budget, h("label", { class: "caption inline" }, "Needed by ", needBy),
        h("button", { class: "btn", type: "submit" }, "Post to suppliers")));
    return group(form, sent, results);
  }

  /* ---------- live requests: buyer posts → supplier pop-up with the D1-D14 deal check ---------- */
  // serve.py stores requests and runs deal_due_diligence for each matched supplier; we poll it.
  const DECISION_TONE = { "GO": "verified", "GO WITH CONDITIONS": "neutral", "ESCALATE": "caution", "NO-GO": "risk" };
  const CHECK_STATUS = { pass: ["Pass", "verified"], condition: ["Condition", "neutral"], escalate: ["Escalate", "caution"], fail: ["Fail", "risk"], info: ["Info", "neutral"] };
  const decisionPill = (check) => !check ? status("Checking…", "neutral")
    : check.error ? status("Check unavailable", "neutral") : status(check.decision, DECISION_TONE[check.decision] || "neutral");
  const requestFacts = (r) => [r.quantity ? `${Number(r.quantity).toLocaleString("en-GB")} ${r.unit}` : null,
    r.budget ? `budget ${money(r.budget, r.currency)}` : null, r.need_by ? `needed by ${r.need_by}` : null].filter(Boolean).join(" · ");

  function postRequest(b, notice, list, terms, sent) {
    if (!list.length) { sent.textContent = "No supplier has proven this item yet, so nothing was sent."; return; }
    sent.textContent = `Sending to ${list.length} supplier agent${list.length > 1 ? "s" : ""}…`;
    fetch("/api/offers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      buyer_id: b.id, title: notice.title, cpv_codes: notice.cpv_codes, currency: "EUR", ...terms,
      matches: list.map((m) => ({ supplier_id: m.supplier.id, item_product: m.item.product })) }) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(() => { sent.textContent = `Sent to ${list.length} supplier agent${list.length > 1 ? "s" : ""}. Each runs the D1–D14 deal check now; results appear below.`; pollRequests(); })
      .catch((err) => { sent.textContent = `Couldn't post the request (${err.message}). Is serve.py running?`; });
  }

  function supplierRequestRows(s) {
    return (state.requests[s.id] || []).slice().reverse().map((r) => {
      const buyer = state.byId.get(r.buyer_id);
      return row(r.title, [`${buyer?.name || r.buyer_id} · ${country(buyer?.country)}${requestFacts(r) ? " · " + requestFacts(r) : ""}`, h("br"),
        `Your match: ${r.match.item_product} · `, decisionPill(r.match.check)],
      h("div", { class: "row-actions" },
        r.match.check?.checks ? h("button", { class: "btn btn-quiet", type: "button", onclick: () => showChecks(r, r.match) }, "Checks") : null,
        h("button", { class: "btn", type: "button", onclick: () => respondToRequest(r) }, "Let agent respond")));
    });
  }

  function buyerRequestRows(b) {
    return (state.requests[b.id] || []).slice().reverse().map((r) => row(r.title,
      [requestFacts(r) || r.created_at.replace("T", " "), h("br"), ...r.matches.flatMap((m, i) => [i ? " · " : "",
        `${state.byId.get(m.supplier_id)?.name || m.supplier_id} `, decisionPill(m.check)])],
      status(`${r.matches.length} supplier${r.matches.length === 1 ? "" : "s"}`, "neutral")));
  }

  function respondToRequest(r) {
    const supplier = state.byId.get(r.match ? sessionCompany()?.id : r.matches[0].supplier_id);
    const buyer = state.byId.get(r.buyer_id);
    if (!supplier || !buyer) return;
    const item = (supplier.catalogue?.items || []).find((i) => i.product === r.match?.item_product) || supplier.catalogue?.items?.[0];
    const notice = { notice_number: r.id, title: r.title, description: "", cpv_codes: r.cpv_codes, published: r.created_at.slice(0, 10), ted_url: "" };
    openMatch({ buyer, supplier, notice, item, score: 1 });
  }

  function showChecks(r, match) {
    const buyer = state.byId.get(r.buyer_id);
    const dialog = h("dialog", { class: "checks-dialog", "aria-labelledby": "checks-title", onclose: () => dialog.remove() },
      h("div", { class: "checks-head" },
        h("div", {}, h("h2", { id: "checks-title" }, "Deal check D1–D14"),
          h("p", { class: "secondary" }, `${r.title} · ${buyer?.name || r.buyer_id} → ${state.byId.get(match.supplier_id)?.name || match.supplier_id}`)),
        h("button", { class: "btn btn-quiet", type: "button", "aria-label": "Close", onclick: () => dialog.close() }, "Close")),
      h("p", {}, decisionPill(match.check), match.check.blocking.length ? ` ${match.check.blocking.length} item(s) need a person` : ` ${match.check.conditions.length} condition(s) for the contract`),
      h("ul", { class: "rows" }, match.check.checks.map((c) => h("li", { class: "row" },
        h("div", { class: "row-main" },
          h("div", { class: "row-title wrap" }, `${c.code} · ${c.title}`),
          h("div", { class: "row-sub wrap" }, c.summary),
          c.conditions.length ? h("ul", { class: "conditions" }, c.conditions.map((x) => h("li", {}, x))) : null),
        h("div", { class: "row-end" }, status(...(CHECK_STATUS[c.status] || [c.status, "neutral"])))))),
      h("p", { class: "caption" }, "Rule tables (Military List, ADR, embargoes, licences) encode common EU practice and are not legal advice."));
    document.body.append(dialog);
    dialog.showModal();
  }

  function toastStack() {
    let stack = document.getElementById("toast-stack");
    if (!stack) document.body.append(stack = h("div", { id: "toast-stack", class: "toast-stack", "aria-live": "polite" }));
    return stack;
  }

  function popRequest(r) {
    const buyer = state.byId.get(r.buyer_id);
    const check = r.match.check;
    const reasons = check?.blocking?.length ? check.blocking : (check?.conditions || []);
    const toast = h("div", { class: "toast", role: "status" },
      h("div", { class: "toast-head" },
        buyer ? avatar(buyer, "sm") : null,
        h("div", { class: "toast-who" }, h("span", { class: "caption" }, "New request from a buyer"),
          h("b", {}, buyer?.name || r.buyer_id), h("span", { class: "caption" }, country(buyer?.country))),
        h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss", onclick: () => toast.remove() }, "×")),
      h("div", { class: "toast-title" }, r.title),
      requestFacts(r) ? h("div", { class: "secondary" }, requestFacts(r)) : null,
      h("div", { class: "secondary" }, `Your match: ${r.match.item_product}`),
      h("div", { class: "toast-check" }, h("span", { class: "caption" }, "Deal check D1–D14 "), decisionPill(check)),
      reasons.length ? h("ul", { class: "conditions" }, reasons.slice(0, 3).map((x) => h("li", {}, trim(x, 110))),
        reasons.length > 3 ? h("li", { class: "secondary" }, `+${reasons.length - 3} more`) : null) : null,
      h("div", { class: "toast-actions" },
        check?.checks ? h("button", { class: "btn btn-quiet", type: "button", onclick: () => showChecks(r, r.match) }, "View checks") : null,
        h("button", { class: "btn", type: "button", onclick: () => { toast.remove(); respondToRequest(r); } }, "Let agent respond")));
    toastStack().prepend(toast);
  }

  const SEEN_KEY = "salespatriot.seenRequests";
  function seenSet(companyId) {
    try { return new Set(JSON.parse(localStorage.getItem(`${SEEN_KEY}.${companyId}`) || "[]")); } catch { return new Set(); }
  }
  function saveSeen(companyId, seen) {
    try { localStorage.setItem(`${SEEN_KEY}.${companyId}`, JSON.stringify([...seen])); } catch { /* storage blocked: may pop again after reload */ }
  }

  async function pollRequests() {
    const c = sessionCompany();
    if (!c) return;
    const param = c.role === "supply" ? "supplier_id" : "buyer_id";
    let list;
    try {
      const r = await fetch(`/api/offers?${param}=${encodeURIComponent(c.id)}`);
      if (!r.ok) return;
      list = await r.json();
    } catch { return; /* server without the API: no live requests */ }
    const before = JSON.stringify(state.requests[c.id] || []);
    state.requests[c.id] = list;
    if (c.role === "supply") {
      // Requests that existed when this browser first looked are history, not news.
      const firstLook = !state.requestsSeeded.has(c.id) && seenSet(c.id).size === 0;
      state.requestsSeeded.add(c.id);
      const seen = seenSet(c.id);
      for (const r of list) {
        if (seen.has(r.id) || !r.match.check) continue;  // pop up once the deal check is ready
        seen.add(r.id);
        if (!firstLook) popRequest(r);
      }
      saveSeen(c.id, seen);
    }
    if (JSON.stringify(list) !== before) refreshLive();
  }

  /* ---------- conversations ---------- */
  const convId = (m) => `deal:${m.buyer.id}:${m.supplier.id}`;
  const MATCHMAKER = { id: "platform", name: "Matchmaking agent", initials: "MA" };

  function openMatch(m) {
    const id = convId(m);
    if (!state.convs.has(id)) {
      const script = A.buildDeal(m);
      const reference = script.referee ? { id: `ref:${script.referee.name}`, name: script.referee.name } : null;
      state.convs.set(id, newConv(id, { buyer: m.buyer, supplier: m.supplier, reference }, script, trim(m.item.product, 44)));
    }
    showConv(id);
  }

  function openFor(company) {
    const match = state.matches.find((m) => m.supplier.id === company.id || m.buyer.id === company.id);
    if (match) return openMatch(match);
    const id = `intro:${company.id}`;
    // Intro chats: the company sits on the left, the matchmaking agent on the right.
    if (!state.convs.has(id)) state.convs.set(id, newConv(id, { buyer: MATCHMAKER, supplier: company }, A.buildIntro(company), "Matchmaking"));
    showConv(id);
  }

  function newConv(id, people, script, topic) {
    // "me" is the side the signed-in company is on; its messages sit on the right.
    const me = sessionCompany()?.id === people.supplier.id ? "supplier" : "buyer";
    const conv = { id, people, script, topic, me, idx: 0, unread: 0, minimized: false, waiting: false, lastFrom: null };
    conv.body = h("div", { class: "chat-body", role: "log", "aria-live": "polite" });
    conv.subtitle = h("span", {}, `Connecting · ${topic}`);
    conv.el = buildChat(conv);
    setTimeout(() => play(conv), 400);
    setTimeout(refreshLive);
    return conv;
  }

  function buildChat(conv) {
    const { buyer, supplier } = conv.people;
    const company = supplier.id === "platform" ? buyer : supplier;
    const title = `${A.shortName(buyer.name, 20)} ↔ ${A.shortName(supplier.name, 20)}`;
    const mine = conv.people[conv.me];
    const input = h("input", { type: "text", placeholder: `Message as ${A.shortName(mine.name, 20)}'s team`, "aria-label": "Message" });
    const send = () => {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      appendEvent(conv, { from: "human", type: "text", text });
      setTimeout(() => appendEvent(conv, { from: conv.me, type: "text",
        text: "Noted. I stay inside the mandate; anything outside it comes back to you." }), 900);
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    // "enter" animates only the first appearance; re-inserting the window must not replay it.
    return h("section", { class: "chat enter", "aria-label": `Agent conversation: ${title}`, onanimationend: (e) => e.currentTarget.classList.remove("enter") },
      h("div", { class: "chat-header", onclick: (e) => { if (!e.target.closest("button, a")) toggleMin(conv); } },
        h("div", { class: "chat-title" }, h("b", {}, title), conv.subtitle),
        link(pathFor(company), { class: "icon-btn", "aria-label": `Open ${company.name}`, title: "Company page" }, "ⓘ"),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Minimize", title: "Minimize", onclick: () => toggleMin(conv) }, "–"),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close", title: "Close", onclick: () => closeConv(conv) }, "×")),
      conv.body,
      h("div", { class: "chat-input" }, input, h("button", { class: "btn btn-quiet", type: "button", onclick: send }, "Send")));
  }

  const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function play(conv) {
    if (conv.waiting || conv.idx >= conv.script.events.length) return;
    const ev = conv.script.events[conv.idx];
    const speaks = ev.type === "text" && ev.from !== "system";
    let delay = ev.type === "system" ? 700 : Math.min(2600, 900 + (ev.text || "").length * 12);
    if (reducedMotion()) delay = 300;
    let typing = null;
    if (speaks) {
      typing = h("div", { class: `typing ${ev.from === conv.me ? "right" : ""}` }, avatar(person(conv, ev.from), "sm"), h("span", { class: "dots", "aria-hidden": "true" }, h("i"), h("i"), h("i")));
      conv.body.append(typing);
      scrollDown(conv);
    }
    setTimeout(() => {
      typing?.remove();
      appendEvent(conv, ev);
      conv.idx += 1;
      if (ev.kind === "approval") { conv.waiting = true; setStatus(conv, "Waiting for approval"); return; }
      play(conv);
    }, delay);
  }

  function person(conv, from) {
    if (from === "reference") return conv.people.reference || { id: "ref", name: "Reference agent" };
    if (from === "human") return { id: "human", name: "You", initials: "You" };
    return conv.people[from] || MATCHMAKER;
  }

  function appendEvent(conv, ev) {
    trackStatus(conv, ev);
    if (ev.type === "system") {
      conv.body.append(h("div", { class: "sys" }, ev.text));
      conv.lastFrom = null;
    } else if (ev.type === "card") {
      conv.body.append(renderCard(conv, ev));
      conv.lastFrom = null;
    } else {
      const who = person(conv, ev.from);
      const first = conv.lastFrom !== ev.from;
      const right = ev.from === conv.me || ev.from === "human";
      conv.body.append(h("div", { class: `msg ${right ? "right" : ""} ${ev.from === "reference" ? "ref" : ""}` },
        first ? avatar(who, "sm") : h("span", { class: "spacer" }),
        h("div", { class: "msg-col" },
          first ? h("span", { class: "msg-from" }, ev.from === "human" ? "You" : A.shortName(who.name, 34)) : null,
          h("div", { class: "bubble" }, ev.text),
          ev.evidence?.length ? h("div", { class: "msg-meta" }, ev.evidence.map(evidenceChip)) : null)));
      conv.lastFrom = ev.from;
    }
    if (conv.minimized || !state.open.includes(conv.id)) {
      conv.unread += 1;
      conv.el.classList.add("unread");
      renderHeads();
    }
    scrollDown(conv);
  }

  function evidenceChip(e) {
    if (e.url) return external(e.url, e.label);
    if (e.kind === "verified") return status("Verified on TED", "verified");
    if (e.kind === "inferred") return status("Demo reference, not real feedback", "caution");
    if (e.kind === "declared") return status("Declared by the company", "neutral");
    return h("span", {}, e.label);
  }

  function renderCard(conv, ev) {
    const d = ev.data;
    const tone = { deal: ["Closed", "verified"], approval: ["Needs approval", "caution"], warn: ["Review", "caution"] }[ev.kind];
    const el = h("div", { class: "card" },
      h("div", { class: "card-title" }, h("span", {}, d.title), tone ? status(...tone) : null),
      d.rows?.length ? h("dl", {}, d.rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])) : null,
      d.audit ? h("ol", {}, d.audit.map((a) => h("li", {}, a))) : null,
      d.note ? h("div", { class: "note" }, d.note) : null);
    if (ev.kind === "approval" && conv.me === "supplier") {
      // The approval belongs to the buyer's team; the supplier sees it happen.
      el.append(h("div", { class: "note" }, "Waiting for the buyer's team to approve."));
      setTimeout(() => {
        appendEvent(conv, { from: "buyer", type: "text", text: "Our procurement officer has approved the deal." });
        conv.waiting = false;
        refreshLive();
        play(conv);
      }, 3000);
    } else if (ev.kind === "approval") {
      const btn = h("button", { class: "btn", type: "button", onclick: () => {
        btn.disabled = true;
        btn.textContent = "Approved";
        appendEvent(conv, { from: "human", type: "text", text: "Approved. I've reviewed the flags; go ahead." });
        conv.waiting = false;
        refreshLive();
        play(conv);
      } }, "Approve");
      el.append(btn);
    }
    return el;
  }

  function trackStatus(conv, ev) {
    const text = ev.text || "";
    if (ev.kind === "mandate") setStatus(conv, "Mandate shared");
    else if (/RFQ issued/.test(ev.data?.title || "")) setStatus(conv, "Request for quote sent");
    else if (ev.kind === "info" || ev.kind === "warn") setStatus(conv, "Due diligence");
    else if (/joined the conversation/.test(text)) setStatus(conv, "Reference check");
    else if (ev.kind === "quote") setStatus(conv, "Negotiating, round 1");
    else if (/Counter-offer/.test(text)) setStatus(conv, "Negotiating, round 2");
    else if (ev.kind === "deal") setStatus(conv, "Deal closed");
    else if (/^Connecting/.test(conv.subtitle.textContent)) setStatus(conv, "Negotiating");
  }

  function setStatus(conv, text) {
    conv.subtitle.textContent = `${text} · ${conv.topic}`;
    refreshLive();
  }

  function scrollDown(conv) { requestAnimationFrame(() => { conv.body.scrollTop = conv.body.scrollHeight; }); }

  function showConv(id) {
    const conv = state.convs.get(id);
    state.open = state.open.filter((x) => x !== id).concat(id);
    while (state.open.length > MAX_OPEN()) state.open.shift();
    conv.minimized = false;
    conv.unread = 0;
    conv.el.classList.remove("min", "unread");
    renderDock();
    scrollDown(conv);
  }

  function toggleMin(conv) {
    conv.minimized = !conv.minimized;
    conv.el.classList.toggle("min", conv.minimized);
    if (!conv.minimized) { conv.unread = 0; conv.el.classList.remove("unread"); scrollDown(conv); }
  }

  function closeConv(conv) {
    state.open = state.open.filter((x) => x !== conv.id);
    renderDock();
  }

  function renderDock() {
    document.getElementById("chat-dock").replaceChildren(...state.open.map((id) => state.convs.get(id).el));
    renderHeads();
  }

  function renderHeads() {
    const heads = [...state.convs.values()].filter((c) => !state.open.includes(c.id));
    document.getElementById("chat-heads").replaceChildren(...heads.map((c) => {
      const company = c.people.supplier.id === "platform" ? c.people.buyer : c.people.supplier;
      return h("button", { class: "chat-head", type: "button", "aria-label": `Open conversation with ${company.name}`, title: company.name, onclick: () => showConv(c.id) },
        avatar(company), c.unread ? h("span", { class: "count" }, c.unread) : null);
    }));
  }

  function runDemo() {
    state.demo.slice(0, MAX_OPEN()).forEach((m, i) => setTimeout(() => openMatch(m), i * 600));
  }

  /* ---------- boot ---------- */
  // In-app links navigate without a reload, so open chats survive page changes.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || a.target === "_blank" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.pathname.startsWith("/data/") || a.getAttribute("href").startsWith("#")) return;
    e.preventDefault();
    navigate(url.pathname + url.search);
  });
  window.addEventListener("popstate", route);
  window.addEventListener("resize", () => { while (state.open.length > MAX_OPEN()) state.open.shift(); renderDock(); });
  load().then(() => {
    restoreSession();
    // ?as=<profile id> signs in as that company (handy for demos and links).
    const as = new URLSearchParams(location.search).get("as");
    if (as && state.byId.has(as)) state.sessionId = as, setSession(as);
    renderSession();
    // Hash links from the first version (#/suppliers, #/company/<id>) still work.
    const old = location.hash.match(/^#\/(suppliers|buyers|company\/(.+))$/);
    if (old) {
      const c = old[2] && state.byId.get(decodeURIComponent(old[2]));
      history.replaceState({}, "", c ? pathFor(c) : `/${old[1]}`);
    }
    route();
    pollRequests();
    setInterval(pollRequests, 2000);
    // ?demo opens the demo negotiations on load (handy for presentations).
    if (new URLSearchParams(location.search).has("demo")) runDemo();
  }).catch((err) => {
    document.getElementById("main").replaceChildren(h("div", { class: "page" },
      h("h1", {}, "Couldn't load the profiles"),
      h("p", { class: "secondary" }, `${err.message || err}. Start the local server from the repository root, then open http://127.0.0.1:8000/:`),
      h("pre", {}, "python3 serve.py")));
  });
})();
