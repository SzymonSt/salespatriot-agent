"""Free open-data sources for supplier due diligence.

Every fetcher returns plain dicts and never raises on a missing record: it returns None
(not found) or {"error": ...} (source unreachable), so the scorer can tell "no data"
from "bad data". All HTTP responses are cached on disk so a re-score is offline and fast.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = "salespatriot-supplier-dd/0.1 (open-data due diligence)"
CACHE_DIR = Path(".cache/dd")
# Registries change slowly; sanctions lists daily.
TTL_RECORD = 7 * 86400
TTL_LIST = 86400
OFFLINE = False  # set by the CLI: use cached responses only

ISO3_TO_ISO2 = {
    "AUT": "AT", "BEL": "BE", "BGR": "BG", "HRV": "HR", "CYP": "CY", "CZE": "CZ",
    "DNK": "DK", "EST": "EE", "FIN": "FI", "FRA": "FR", "DEU": "DE", "GRC": "EL",
    "HUN": "HU", "IRL": "IE", "ITA": "IT", "LVA": "LV", "LTU": "LT", "LUX": "LU",
    "MLT": "MT", "NLD": "NL", "POL": "PL", "PRT": "PT", "ROU": "RO", "SVK": "SK",
    "SVN": "SI", "ESP": "ES", "SWE": "SE", "GBR": "GB", "NOR": "NO", "CHE": "CH",
    "ZAF": "ZA", "USA": "US", "UKR": "UA",
}

LV_DATA = "https://data.gov.lv/dati/dataset"
LV_FILES = {
    "register": f"{LV_DATA}/4de9697f-850b-45ec-8bba-61fa09ce932f/resource/25e80bf3-f107-4ab4-89ef-251b5b9374e9/download/register.csv",
    "members": f"{LV_DATA}/e1162626-e02a-4545-9236-37553609a988/resource/837b451a-4833-4fd1-bfdd-b45b35a994fd/download/members.csv",
    "beneficial_owners": f"{LV_DATA}/b7848ab9-7886-4df0-8bc6-70052a8d9e1a/resource/20a9b26d-d056-4dbb-ae18-9ff23c87bdee/download/beneficial_owners.csv",
    "insolvency": f"{LV_DATA}/bb54838d-9365-4f76-8513-804f4efa8ab6/resource/8065ad80-1a4d-4afb-b1d1-d93b9a62b1cc/download/insolvency_legal_person_proceeding.csv",
    "suspensions": f"{LV_DATA}/c35c2fce-0014-4664-957f-b71c2f56acae/resource/1a077ff6-4c4e-4cfe-b603-b7e40851e8a9/download/suspensions_prohibitions.csv",
    "financial_statements": f"{LV_DATA}/8d31b878-536a-44aa-a013-8bc6b669d477/resource/27fcc5ec-c63b-4bfd-bb08-01f073a52d04/download/financial_statements.csv",
    "income_statements": f"{LV_DATA}/8d31b878-536a-44aa-a013-8bc6b669d477/resource/d5fd17ef-d32e-40cb-8399-82b780095af0/download/income_statements.csv",
    "balance_sheets": f"{LV_DATA}/8d31b878-536a-44aa-a013-8bc6b669d477/resource/50ef4f26-f410-4007-b296-22043ca3dc43/download/balance_sheets.csv",
}

SANCTIONS_LISTS = {
    "EU": "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
    "UN": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
    "OFAC_SDN": "https://www.treasury.gov/ofac/downloads/sdn.csv",
    "OFAC_ALT": "https://www.treasury.gov/ofac/downloads/alt.csv",
    "OFAC_CONS": "https://www.treasury.gov/ofac/downloads/consolidated/cons_prim.csv",
    "UK": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",
}

LEGAL_FORMS = {
    "sro", "s r o", "spol", "sp", "z", "oo", "zoo", "spolka", "spolkaz", "ograniczona",
    "ograniczonaodpowiedzialnoscia", "odpowiedzialnoscia", "z", "gmbh", "ag", "kg", "co",
    "spa", "srl", "sl", "slu", "sa", "sas", "sarl", "as", "a", "ab", "bv", "nv", "ltd",
    "limited", "plc", "ou", "sia", "doo", "lda", "ood", "eood", "ad", "oy", "aps", "llc",
    "inc", "pty", "the", "sabiedriba", "ar", "ierobezotu", "atbildibu", "storitve", "in",
    "trgovina", "international", "group", "holding", "spzoo", "ltda", "kft", "zrt",
}


# --------------------------------------------------------------------------- HTTP


def _cache_path(key: str, suffix: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{digest}{suffix}"


def http(
    url: str,
    *,
    data: Any = None,
    headers: dict[str, str] | None = None,
    ttl: int = TTL_RECORD,
    binary: bool = False,
    timeout: int = 60,
    min_interval: float = 0.0,
) -> tuple[Any, str]:
    """GET/POST with disk cache. Returns (payload, observed_at ISO date).

    payload is parsed JSON when the response is JSON, text otherwise, bytes if binary.
    Returns ({"error": msg}, now) on failure.
    """
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    key = url + (body.decode("utf-8", "replace") if body else "")
    path = _cache_path(key, ".bin")
    meta = path.with_suffix(".meta")
    if path.exists() and meta.exists():
        info = json.loads(meta.read_text())
        if OFFLINE or time.time() - info["fetched"] < ttl:
            return _decode(path.read_bytes(), info.get("type", ""), binary), info["observed_at"]
    if OFFLINE:
        return {"error": "offline and not cached"}, today()
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = Request(url, data=body, headers=request_headers, method="POST" if body else "GET")
    for attempt in range(4):
        if min_interval:
            time.sleep(min_interval)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
            break
        except HTTPError as error:
            if error.code == 404:
                return None, today()
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": f"HTTP {error.code}"}, today()
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return {"error": str(error)[:120]}, today()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    observed = today()
    meta.write_text(json.dumps({"url": url, "fetched": time.time(), "observed_at": observed, "type": content_type}))
    return _decode(raw, content_type, binary), observed


def _decode(raw: bytes, content_type: str, binary: bool) -> Any:
    if binary:
        return raw
    text = raw.decode("utf-8-sig", "replace")
    if "json" in content_type or text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text


def today() -> str:
    return time.strftime("%Y-%m-%d")


def is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and set(payload) == {"error"}


# --------------------------------------------------------------------------- names


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower().replace("ł", "l").replace("ø", "o").replace("æ", "ae").replace("ß", "ss")


def name_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", fold(name))
    core = [w for w in words if w not in LEGAL_FORMS]
    return core or words


def norm_name(name: str) -> str:
    return " ".join(name_tokens(name))


def name_similarity(a: str, b: str) -> float:
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def evidence(source: str, url: str, observed_at: str, fact: str, status: str = "document_verified") -> dict[str, str]:
    return {"source": source, "source_url": url, "observed_at": observed_at, "fact": fact, "verification_status": status}


# --------------------------------------------------------------------------- national registers
# Each returns a normalised "registry record":
# {source, url, observed_at, name, status_active, status_text, founded, address, activity_codes,
#  activity_text, owners:[{name,country,share,kind}], officers:[names], insolvency:bool|None,
#  financials:{year, currency, revenue, net_profit, equity, employees}|None, filings_last_year,
#  extra:{}}


def _record(**kwargs: Any) -> dict[str, Any]:
    base = {
        "name": "", "status_active": None, "status_text": "", "founded": "", "address": "",
        "activity_codes": [], "activity_text": [], "owners": None, "officers": [],
        "insolvency": None, "financials": None, "filings_last_year": None, "extra": {},
    }
    base.update(kwargs)
    return base


def cz_ares(ico: str) -> dict[str, Any] | None:
    ico = re.sub(r"\D", "", ico).zfill(8)
    url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
    basic, observed = http(url)
    if basic is None or is_error(basic):
        return basic
    vr, _ = http(f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-vr/{ico}")
    registrations = basic.get("seznamRegistraci", {})
    rec = _record(
        source="ARES (CZ business register)",
        url=f"https://ares.gov.cz/ekonomicke-subjekty?ico={ico}",
        observed_at=observed,
        name=basic.get("obchodniJmeno", ""),
        founded=basic.get("datumVzniku", ""),
        address=(basic.get("sidlo") or {}).get("textovaAdresa", ""),
        activity_codes=sorted({c for c in basic.get("czNace2008", []) + basic.get("czNace", []) if c}),
        insolvency=registrations.get("stavZdrojeIr") == "AKTIVNI",
        extra={"vat": basic.get("dic", "")},
    )
    rec["status_active"] = basic.get("datumZaniku") in (None, "") and registrations.get("stavZdrojeVr") != "ZANIKLY"
    rec["status_text"] = "active" if rec["status_active"] else "dissolved"
    if isinstance(vr, dict) and vr.get("zaznamy"):
        entry = next((z for z in vr["zaznamy"] if z.get("primarniZaznam")), vr["zaznamy"][0])
        rec["status_text"] = {"AKTIVNI": "active", "ZANIKLY": "dissolved"}.get(entry.get("stavSubjektu", ""), rec["status_text"])
        rec["status_active"] = entry.get("stavSubjektu") == "AKTIVNI"
        rec["activity_text"] = [
            p["hodnota"].replace("\n", " ")
            for p in (entry.get("cinnosti") or {}).get("predmetPodnikani", [])
            if not p.get("datumVymazu")
        ]
        owners = []
        for group in entry.get("spolecnici", []):
            if group.get("datumVymazu"):
                continue
            for member in group.get("spolecnik", []):
                if member.get("datumVymazu"):
                    continue
                person = member.get("osoba", {})
                legal, natural = person.get("pravnickaOsoba"), person.get("fyzickaOsoba")
                holder = legal or natural or {}
                share = next(
                    (p.get("velikostPodilu", {}).get("hodnota") for p in reversed(member.get("podil", [])) if not p.get("datumVymazu")),
                    None,
                )
                owners.append({
                    "name": holder.get("obchodniJmeno") or " ".join(filter(None, [holder.get("jmeno"), holder.get("prijmeni")])),
                    "country": ((holder.get("adresa") or {}).get("kodStatu") or "").upper(),
                    "share": share,
                    "kind": "company" if legal else "person",
                })
        rec["owners"] = owners
        for body in entry.get("statutarniOrgany", []):
            for member in body.get("clenoveOrganu", []):
                if member.get("datumVymazu"):
                    continue
                person = member.get("fyzickaOsoba") or member.get("pravnickaOsoba") or {}
                name = person.get("obchodniJmeno") or " ".join(filter(None, [person.get("jmeno"), person.get("prijmeni")]))
                if name:
                    rec["officers"].append(name)
    return rec


def pl_registers(nip: str) -> dict[str, Any] | None:
    nip = re.sub(r"\D", "", nip)
    url = f"https://wl-api.mf.gov.pl/api/search/nip/{nip}?date={today()}"
    wl, observed = http(url)
    if wl is None or is_error(wl):
        return wl
    subject = (wl.get("result") or {}).get("subject")
    if not subject:
        return None
    rec = _record(
        source="Polish VAT white list + KRS",
        url=f"https://wl-api.mf.gov.pl/api/search/nip/{nip}",
        observed_at=observed,
        name=subject.get("name", ""),
        status_active=subject.get("statusVat") == "Czynny",
        status_text=f"VAT status: {subject.get('statusVat')}",
        founded=subject.get("registrationLegalDate") or "",
        address=subject.get("residenceAddress") or subject.get("workingAddress") or "",
        extra={"krs": subject.get("krs"), "regon": subject.get("regon")},
    )
    krs = subject.get("krs")
    if not krs:
        return rec
    odpis, _ = http(f"https://api-krs.ms.gov.pl/api/krs/OdpisAktualny/{krs}?rejestr=P&format=json")
    if not isinstance(odpis, dict) or "odpis" not in odpis:
        return rec
    header, data = odpis["odpis"]["naglowekA"], odpis["odpis"]["dane"]
    rec["url"] = f"https://wyszukiwarka-krs.ms.gov.pl/ (KRS {krs})"
    rec["founded"] = _pl_date(header.get("dataRejestracjiWKRS", "")) or rec["founded"]
    activity = (data.get("dzial3") or {}).get("przedmiotDzialalnosci", {})
    for section in ("przedmiotPrzewazajacejDzialalnosci", "przedmiotPozostalejDzialalnosci"):
        for item in activity.get(section, []):
            code = "".join(filter(None, [item.get("kodDzial"), item.get("kodKlasa"), item.get("kodPodklasa")]))
            rec["activity_codes"].append(code)
            rec["activity_text"].append(item.get("opis", ""))
    owners = []
    for holder in (data.get("dzial1") or {}).get("wspolnicySpzoo", []) or []:
        is_company = bool(holder.get("nazwa"))
        owners.append({
            "name": holder.get("nazwa") or "(natural person, name masked by KRS)",
            "country": "PL" if (holder.get("krs") or (holder.get("identyfikator") or {}).get("regon")) else "",
            "share": holder.get("posiadaneUdzialy"),
            "kind": "company" if is_company else "person",
        })
    rec["owners"] = owners
    # dzial6 lists liquidation, bankruptcy and restructuring; empty means none registered.
    dz6 = data.get("dzial6") or {}
    rec["insolvency"] = any(re.search(r"likwid|upadl|restruktur|sanacj|rozwiazan", k.lower()) and v for k, v in dz6.items())
    filings = ((data.get("dzial3") or {}).get("wzmiankiOZlozonychDokumentach") or {}).get("wzmiankaOZlozeniuRocznegoSprawozdaniaFinansowego", [])
    years = [int(y) for f in filings for y in re.findall(r"(20\d\d)", f.get("zaOkresOdDo", ""))[-1:]]
    rec["filings_last_year"] = max(years) if years else None
    arrears = (data.get("dzial4") or {})
    rec["extra"]["registered_arrears"] = bool(arrears.get("zaleglosci") or arrears.get("wierzytelnosci"))
    return rec


def _pl_date(value: str) -> str:
    match = re.match(r"(\d\d)\.(\d\d)\.(\d{4})", value or "")
    return f"{match[3]}-{match[2]}-{match[1]}" if match else ""


def fr_sirene(identifier: str) -> dict[str, Any] | None:
    siren = re.sub(r"\D", "", identifier)[:9]
    url = f"https://recherche-entreprises.api.gouv.fr/search?q={siren}"
    data, observed = http(url)
    if data is None or is_error(data):
        return data
    hit = next((r for r in data.get("results", []) if r.get("siren") == siren), None)
    if not hit:
        return None
    finances = hit.get("finances") or {}
    fin = None
    if finances:
        year = max(finances)
        fin = {"year": int(year), "currency": "EUR", "revenue": finances[year].get("ca"), "net_profit": finances[year].get("resultat_net")}
    return _record(
        source="SIRENE / Recherche d'entreprises (FR)",
        url=f"https://annuaire-entreprises.data.gouv.fr/entreprise/{siren}",
        observed_at=observed,
        name=hit.get("nom_raison_sociale") or hit.get("nom_complet", ""),
        status_active=hit.get("etat_administratif") == "A",
        status_text="active" if hit.get("etat_administratif") == "A" else "ceased",
        founded=hit.get("date_creation") or "",
        address=(hit.get("siege") or {}).get("adresse", ""),
        activity_codes=[hit.get("activite_principale", "")],
        owners=None,  # French register API does not expose shareholders
        officers=[d.get("denomination") or " ".join(filter(None, [d.get("prenoms"), d.get("nom")])) for d in hit.get("dirigeants", []) if d.get("type_dirigeant") == "personne physique"],
        insolvency=None,
        financials=fin,
        filings_last_year=fin["year"] if fin else None,
        extra={"size_category": hit.get("categorie_entreprise"), "employee_band": hit.get("tranche_effectif_salarie")},
    )


def ro_anaf(cui: str) -> dict[str, Any] | None:
    cui_num = int(re.sub(r"\D", "", cui) or 0)
    url = "https://webservicesp.anaf.ro/api/PlatitorTvaRest/v9/tva"
    data, observed = http(url, data=[{"cui": cui_num, "data": today()}], ttl=TTL_RECORD)
    if data is None or is_error(data) or not isinstance(data, dict) or not data.get("found"):
        return data if is_error(data) else None
    found = data["found"][0]
    general, inactive = found["date_generale"], found.get("stare_inactiv", {})
    status_active = not inactive.get("statusInactivi") and not inactive.get("dataRadiere")
    rec = _record(
        source="ANAF (RO tax register)",
        url=f"https://www.anaf.ro (CUI {cui_num})",
        observed_at=observed,
        name=general.get("denumire", ""),
        status_active=status_active,
        status_text=general.get("stare_inregistrare", "") + (" — INACTIVE taxpayer" if inactive.get("statusInactivi") else ""),
        founded=general.get("data_inregistrare", ""),
        address=general.get("adresa", ""),
        activity_codes=[general.get("cod_CAEN", "")],
        extra={"ownership_form": general.get("forma_de_proprietate", ""), "trade_register_no": general.get("nrRegCom", "")},
    )
    if "STRAIN" in fold(general.get("forma_de_proprietate", "")).upper():
        rec["owners"] = [{"name": "(foreign capital — names not in ANAF data)", "country": "", "share": None, "kind": "company"}]
    for year in (int(today()[:4]) - 1, int(today()[:4]) - 2):
        bilant, _ = http(f"https://webservicesp.anaf.ro/bilant?an={year}&cui={cui_num}")
        if isinstance(bilant, dict) and bilant.get("i"):
            values = {i["indicator"]: i["val_indicator"] for i in bilant["i"]}
            if any(values.values()):
                rec["financials"] = {
                    "year": year, "currency": "RON", "revenue": values.get("I13"),
                    "net_profit": (values.get("I18") or 0) - (values.get("I19") or 0),
                    "equity": values.get("I10"), "employees": values.get("I20"),
                }
                rec["filings_last_year"] = year
                break
    return rec


def sk_registers(ico: str) -> dict[str, Any] | None:
    ico = re.sub(r"\D", "", ico).zfill(8)
    search, observed = http(f"https://api.statistics.sk/rpo/v1/search?identifier={ico}")
    if search is None or is_error(search) or not search.get("results"):
        return search if is_error(search) else None
    entity_id = search["results"][0]["id"]
    detail, _ = http(f"https://api.statistics.sk/rpo/v1/entity/{entity_id}?showHistoricalData=false")
    if not isinstance(detail, dict):
        return None

    def current(items: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [i for i in (items or []) if not i.get("validTo")]

    names = current(detail.get("fullNames"))
    stats = detail.get("statisticalCodes") or {}
    owners = []
    for holder in current(detail.get("stakeholders")):
        kind = (holder.get("stakeholderType") or {}).get("value", "")
        if "Prokurist" in kind:
            continue
        owners.append({
            "name": holder.get("fullName") or " ".join(filter(None, [holder.get("personName", {}).get("givenNames", [""])[0] if holder.get("personName") else "", (holder.get("personName") or {}).get("familyNames", [""])[0]])).strip(),
            "country": _country_code((holder.get("address") or {}).get("country", {}).get("value", "")),
            "share": None,
            "kind": "company" if holder.get("fullName") else "person",
        })
    rec = _record(
        source="RPO (SK register of legal entities)",
        url=f"https://rpo.statistics.sk (IČO {ico})",
        observed_at=observed,
        name=names[0]["value"] if names else "",
        status_active=not detail.get("termination"),
        status_text="terminated" if detail.get("termination") else "active",
        founded=detail.get("establishment", ""),
        activity_codes=[(stats.get("mainActivity") or {}).get("code", "")],
        activity_text=[a.get("economicActivityDescription", "") for a in current(detail.get("activities"))],
        owners=owners or None,
        officers=[
            " ".join(filter(None, [((b.get("personName") or {}).get("givenNames") or [""])[0], ((b.get("personName") or {}).get("familyNames") or [""])[0]])).strip()
            for b in current(detail.get("statutoryBodies"))
        ],
    )
    rpvs, _ = http(f"https://rpvs.gov.sk/opendatav2/PartneriVerejnehoSektora?$filter=Ico%20eq%20'{ico}'")
    if isinstance(rpvs, dict) and rpvs.get("value"):
        partner = rpvs["value"][0]
        rec["extra"]["rpvs_partner_id"] = partner.get("Id")
        rec["extra"]["rpvs_registered"] = not partner.get("PlatnostDo")
        owners_url = f"https://rpvs.gov.sk/opendatav2/PartneriVerejnehoSektora({partner['Id']})?$expand=KonecniUzivateliaVyhod"
        detail_bo, _ = http(owners_url)
        if isinstance(detail_bo, dict):
            ubos = [u for u in detail_bo.get("KonecniUzivateliaVyhod", []) if not u.get("PlatnostDo")]
            rec["extra"]["ubo_count"] = len(ubos)
            rec["extra"]["ubo_public_officials"] = sum(1 for u in ubos if u.get("JeVerejnyCinitel"))
            if rec["owners"] is None and ubos:
                rec["owners"] = [{"name": f"{u.get('Meno', '')} {u.get('Priezvisko', '')}".strip(), "country": "", "share": None, "kind": "ubo"} for u in ubos]
    ruz, _ = http(f"https://www.registeruz.sk/cruz-public/api/uctovne-jednotky?zmenene-od=2000-01-01&ico={ico}")
    if isinstance(ruz, dict) and ruz.get("id"):
        unit, _ = http(f"https://www.registeruz.sk/cruz-public/api/uctovna-jednotka?id={ruz['id'][0]}")
        if isinstance(unit, dict):
            rec["extra"]["employee_band"] = unit.get("velkostOrganizacie")
            closing_ids = unit.get("idUctovnychZavierok") or []
            years = []
            for closing_id in closing_ids[-3:]:
                closing, _ = http(f"https://www.registeruz.sk/cruz-public/api/uctovna-zavierka?id={closing_id}")
                if isinstance(closing, dict) and closing.get("obdobieDo"):
                    years.append(int(closing["obdobieDo"][:4]))
            rec["filings_last_year"] = max(years) if years else None
    return rec


def dk_cvr(cvr: str) -> dict[str, Any] | None:
    cvr = re.sub(r"\D", "", cvr)
    data, observed = http(f"https://cvrapi.dk/api?search={cvr}&country=dk")
    if data is None or is_error(data) or not isinstance(data, dict) or data.get("error"):
        return None if not is_error(data) else data
    founded = ""
    match = re.match(r"(\d\d)/(\d\d) - (\d{4})", data.get("startdate") or "")
    if match:
        founded = f"{match[3]}-{match[2]}-{match[1]}"
    return _record(
        source="CVR (DK business register, via cvrapi.dk)",
        url=f"https://datacvr.virk.dk/enhed/virksomhed/{cvr}",
        observed_at=observed,
        name=data.get("name", ""),
        status_active=not data.get("enddate") and not data.get("creditbankrupt"),
        status_text="bankrupt" if data.get("creditbankrupt") else ("ceased" if data.get("enddate") else "active"),
        founded=founded,
        address=f"{data.get('address', '')}, {data.get('zipcode', '')} {data.get('city', '')}",
        activity_codes=[str(data.get("industrycode", ""))],
        activity_text=[data.get("industrydesc", "")],
        owners=[{"name": o.get("name", ""), "country": "", "share": None, "kind": "unknown"} for o in data.get("owners") or []] or None,
        insolvency=bool(data.get("creditbankrupt")),
        financials={"year": None, "currency": "", "employees": data.get("employees")} if data.get("employees") else None,
    )


def ee_ariregister(code: str) -> dict[str, Any] | None:
    code = re.sub(r"\D", "", code)
    data, observed = http(f"https://ariregister.rik.ee/est/api/autocomplete?q={code}")
    if data is None or is_error(data):
        return data
    hit = next((d for d in data.get("data", []) if str(d.get("reg_code")) == code), None)
    if not hit:
        return None
    status = hit.get("status")
    return _record(
        source="e-Business Register (EE)",
        url=hit.get("url") or f"https://ariregister.rik.ee/eng/company/{code}",
        observed_at=observed,
        name=hit.get("name", ""),
        status_active=status == "R",
        status_text={"R": "registered", "L": "in liquidation", "N": "bankrupt", "K": "deleted"}.get(status, status or ""),
        address=hit.get("legal_address", ""),
        insolvency=status in ("L", "N"),
    )


def lv_registers(regcode: str) -> dict[str, Any] | None:
    regcode = re.sub(r"\D", "", regcode)
    rows = {name: lv_rows(name, regcode) for name in LV_FILES}
    if any(is_error(r) for r in rows.values()):
        return next(r for r in rows.values() if is_error(r))
    register = rows["register"]
    if not register:
        return None
    entry = register[0]
    observed = today()
    owners = [
        {"name": m.get("name", ""), "country": "LV" if m.get("latvian_identity_number_masked") or m.get("legal_entity_registration_number") else "", "share": m.get("number_of_shares"), "kind": m.get("entity_type", "").lower()}
        for m in rows["members"]
    ]
    for bo in rows["beneficial_owners"]:
        owners.append({"name": f"{bo.get('forename', '')} {bo.get('surname', '')}".strip(), "country": bo.get("nationality", ""), "share": None, "kind": "ubo"})
    statements = sorted(rows["financial_statements"], key=lambda r: r.get("year", ""))
    fin = None
    if statements:
        last = statements[-1]
        income = next((r for r in rows["income_statements"] if r.get("statement_id") == last["id"]), {})
        balance = next((r for r in rows["balance_sheets"] if r.get("statement_id") == last["id"]), {})
        fin = {
            "year": int(last["year"]), "currency": last.get("currency") or "EUR",
            "revenue": _num(income.get("net_turnover")), "net_profit": _num(income.get("net_income")),
            "equity": _num(balance.get("equity")), "employees": _num(last.get("employees")),
        }
    open_insolvency = [p for p in rows["insolvency"] if not p.get("proceeding_ended_on")]
    suspensions = [s for s in rows["suspensions"]]
    rec = _record(
        source="UR open data (LV Register of Enterprises)",
        url=f"https://info.ur.gov.lv/#/data-search (reg. no. {regcode})",
        observed_at=observed,
        name=entry.get("name", ""),
        status_active=not entry.get("terminated", "").strip() and not entry.get("closed", "").strip(),
        status_text="terminated" if entry.get("terminated") else "active",
        founded=entry.get("registered", ""),
        address=entry.get("address", ""),
        owners=owners or None,
        insolvency=bool(open_insolvency),
        financials=fin,
        filings_last_year=fin["year"] if fin else None,
        extra={"suspensions": [f"{s.get('suspension_code_text')} from {s.get('date_from')}" for s in suspensions]},
    )
    return rec


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def lv_rows(dataset: str, regcode: str) -> list[dict[str, str]] | dict[str, str]:
    """Stream a large UR CSV once and keep a per-company index on disk."""
    index_path = CACHE_DIR / f"lv_{dataset}_index.json"
    if index_path.exists() and (OFFLINE or time.time() - index_path.stat().st_mtime < TTL_LIST * 7):
        index = json.loads(index_path.read_text())
        if regcode in index.get("_wanted", []):
            return index.get(regcode, [])
    else:
        index = {"_wanted": []}
    if OFFLINE:
        return {"error": "offline and not cached"}
    wanted = set(index["_wanted"]) | {regcode}
    # Statement-level tables are keyed by statement_id, so resolve ids first.
    statement_ids: set[str] = set()
    if dataset in ("income_statements", "balance_sheets"):
        statements = lv_rows("financial_statements", regcode)
        if is_error(statements):
            return statements
        statement_ids = {s["id"] for s in statements}
    key_field = {
        "register": "regcode", "members": "at_legal_entity_registration_number",
        "beneficial_owners": "legal_entity_registration_number", "insolvency": "debtor_registration_number",
        "suspensions": "legal_entity_registration_number", "financial_statements": "legal_entity_registration_number",
        "income_statements": "statement_id", "balance_sheets": "statement_id",
    }[dataset]
    try:
        request = Request(LV_FILES[dataset], headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=600) as response:
            reader = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8-sig"), delimiter=";")
            found: dict[str, list[dict[str, str]]] = {code: [] for code in wanted}
            for row in reader:
                key = row.get(key_field, "")
                if dataset in ("income_statements", "balance_sheets"):
                    if key in statement_ids:
                        found[regcode].append(row)
                elif key in wanted:
                    found[key].append(row)
    except (URLError, HTTPError, TimeoutError, ConnectionError) as error:
        return {"error": f"LV {dataset}: {error}"[:120]}
    found["_wanted"] = sorted(wanted)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(found))
    return found.get(regcode, [])


def _country_code(name: str) -> str:
    name = fold(name)
    table = {
        "slovenska republika": "SK", "slovensko": "SK", "ceska republika": "CZ", "polska": "PL",
        "nemecko": "DE", "rakusko": "AT", "madarsko": "HU", "ruska federacia": "RU", "rusko": "RU",
        "bielorusko": "BY", "cina": "CN", "ukrajina": "UA", "spojene staty americke": "US",
    }
    return table.get(name, "")


NATIONAL_REGISTERS = {
    "CZE": cz_ares, "POL": pl_registers, "FRA": fr_sirene, "ROU": ro_anaf,
    "SVK": sk_registers, "DNK": dk_cvr, "EST": ee_ariregister, "LVA": lv_registers,
}


# --------------------------------------------------------------------------- EU-wide sources


def vies(country_iso3: str, identifier: str) -> dict[str, Any] | None:
    cc = ISO3_TO_ISO2.get(country_iso3, "")
    if not cc or cc in ("GB", "NO", "CH", "ZA", "US", "UA"):
        return None
    number = re.sub(r"[^0-9A-Z]", "", identifier.upper())
    if number.startswith(cc) or (cc == "EL" and number.startswith("GR")):
        number = number[2:]
    if not number:
        return None
    url = f"https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{number}"
    data, observed = http(url, ttl=TTL_RECORD)
    if data is None or is_error(data):
        return data
    if data.get("userError") not in ("VALID", "INVALID"):
        return {"error": f"VIES: {data.get('userError')}"}
    name = data.get("name") if data.get("name") not in (None, "---") else ""
    return {"valid": bool(data.get("isValid")), "vat": f"{cc}{number}", "name": name, "url": url, "observed_at": observed}


def gleif(name: str, country_iso3: str, identifier: str) -> dict[str, Any] | None:
    cc = ISO3_TO_ISO2.get(country_iso3, "")
    cc = "GR" if cc == "EL" else cc
    base = "https://api.gleif.org/api/v1/lei-records"
    candidates = []
    ident = re.sub(r"[^0-9A-Za-z]", "", identifier)
    for params in (
        {"filter[entity.registeredAs]": ident, "filter[entity.legalAddress.country]": cc},
        {"filter[fulltext]": norm_name(name), "filter[entity.legalAddress.country]": cc},
    ):
        if not all(params.values()):
            continue
        data, observed = http(f"{base}?{urllib.parse.urlencode(params)}&page[size]=10", headers={"Accept": "application/vnd.api+json"})
        if is_error(data):
            return data
        for record in (data or {}).get("data", []):
            legal = record["attributes"]["entity"]["legalName"]["name"]
            score = 1.0 if params.get("filter[entity.registeredAs]") else name_similarity(name, legal)
            candidates.append((score, record, observed))
        if candidates and candidates[0][0] == 1.0:
            break
    if not candidates:
        return None
    score, record, observed = max(candidates, key=lambda c: c[0])
    if score < 0.5:
        return None
    attrs = record["attributes"]
    lei = record["id"]
    parents = {}
    for relation in ("direct-parent", "ultimate-parent"):
        parent, _ = http(f"{base}/{lei}/{relation}", headers={"Accept": "application/vnd.api+json"})
        if isinstance(parent, dict) and parent.get("data"):
            entity = parent["data"]["attributes"]["entity"]
            parents[relation] = {"lei": parent["data"]["id"], "name": entity["legalName"]["name"], "country": entity["legalAddress"]["country"]}
    return {
        "lei": lei,
        "name": attrs["entity"]["legalName"]["name"],
        "registered_as": attrs["entity"].get("registeredAs"),
        "entity_status": attrs["entity"]["status"],
        "registration_status": attrs["registration"]["status"],
        "next_renewal": (attrs["registration"].get("nextRenewalDate") or "")[:10],
        "founded": (attrs["entity"].get("creationDate") or "")[:10],
        "country": attrs["entity"]["legalAddress"]["country"],
        "parents": parents,
        "match": "identifier" if score == 1.0 else f"name similarity {score:.2f}",
        "url": f"https://search.gleif.org/#/record/{lei}",
        "observed_at": observed,
    }


def wikidata(lei: str | None, name: str, country_iso3: str) -> dict[str, Any] | None:
    if lei:
        selector = f'?item wdt:P1278 "{lei}" .'
    else:
        # Exact English label in the supplier's country; no fuzzy matching on Wikidata.
        cc = ISO3_TO_ISO2.get(country_iso3, "")
        cc = "GR" if cc == "EL" else cc
        safe = name.replace('"', "").replace("\\", "")
        selector = f'?item rdfs:label "{safe}"@en ; wdt:P17 ?country . ?country wdt:P297 "{cc}" .'
    query = f"""
    SELECT ?item ?itemLabel ?inception ?industryLabel ?parentLabel ?parentCountry ?ownerLabel ?ownerCountry ?employees ?website WHERE {{
      {selector}
      OPTIONAL {{ ?item wdt:P571 ?inception }}
      OPTIONAL {{ ?item wdt:P452 ?industry }}
      OPTIONAL {{ ?item wdt:P749 ?parent . OPTIONAL {{ ?parent wdt:P17 ?pc . ?pc wdt:P297 ?parentCountry }} }}
      OPTIONAL {{ ?item wdt:P127 ?owner . OPTIONAL {{ ?owner wdt:P17 ?oc . ?oc wdt:P297 ?ownerCountry }} }}
      OPTIONAL {{ ?item wdt:P1128 ?employees }}
      OPTIONAL {{ ?item wdt:P856 ?website }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" }}
    }} LIMIT 50"""
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(query)
    data, observed = http(url, headers={"Accept": "application/sparql-results+json"})
    if data is None or is_error(data) or not isinstance(data, dict):
        return data if is_error(data) else None
    rows = data.get("results", {}).get("bindings", [])
    if not rows:
        return None

    def values(key: str) -> list[str]:
        return sorted({r[key]["value"] for r in rows if key in r})

    item = rows[0]["item"]["value"]
    return {
        "qid": item.rsplit("/", 1)[-1],
        "url": item,
        "observed_at": observed,
        "industries": values("industryLabel"),
        "parents": [{"name": n, "country": c} for n, c in {(r["parentLabel"]["value"], r.get("parentCountry", {}).get("value", "")) for r in rows if "parentLabel" in r}],
        "owners": [{"name": n, "country": c} for n, c in {(r["ownerLabel"]["value"], r.get("ownerCountry", {}).get("value", "")) for r in rows if "ownerLabel" in r}],
        "inception": (values("inception") or [""])[0][:10],
        "employees": values("employees"),
        "website": values("website"),
    }


def eu_funding(name: str) -> dict[str, Any] | None:
    """EU Funding & Tenders portal: registered participant (PIC) and EU defence projects."""
    query = urllib.parse.quote(f'"{norm_name(name)}"')
    url = f"https://api.tech.ec.europa.eu/search-api/prod/rest/search?apiKey=SEDIA&text={query}&pageSize=50&pageNumber=1"
    data, observed = http(url, data=b"", ttl=TTL_RECORD)
    if data is None or is_error(data) or not isinstance(data, dict):
        return data if is_error(data) else None
    organisations, projects = [], []
    for result in data.get("results", []):
        meta = result.get("metadata", {})
        title = result.get("title") or result.get("summary") or ""
        if meta.get("type") == ["ORGANISATION"]:
            if name_similarity(name, title) >= 0.6:
                organisations.append({"name": title, "url": result.get("url")})
            continue
        calls = meta.get("callIdentifier") or []
        if any(re.match(r"(EDF|EDIDP|PADR|ASAP|EDIRPA|EDIP)", c) for c in calls):
            projects.append({"title": title, "call": calls[0], "url": result.get("url")})
    unique = {p["url"]: p for p in projects}
    return {"organisations": organisations, "defence_projects": list(unique.values()), "url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/", "observed_at": observed}


def adverse_media(name: str) -> dict[str, Any] | None:
    """GDELT DOC API (last ~3 months of global news). Headlines only; unverified press."""
    terms = "(bribery OR corruption OR fraud OR sanctions OR indicted OR raid OR cartel OR smuggling OR embargo)"
    short = " ".join(name_tokens(name)[:3])
    if len(short) < 4:
        return None
    query = urllib.parse.quote(f'"{short}" {terms}')
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={query}&mode=artlist&format=json&maxrecords=25&sort=datedesc"
    data, observed = http(url, ttl=TTL_RECORD, min_interval=6.5, timeout=40)
    if is_error(data):
        return data
    if not isinstance(data, dict):
        return {"articles": [], "url": url, "observed_at": observed}
    articles = [
        {"title": a.get("title", ""), "url": a.get("url", ""), "date": a.get("seendate", "")[:8], "domain": a.get("domain", "")}
        for a in data.get("articles", [])
        if set(name_tokens(name)[:2]) <= set(name_tokens(a.get("title", "")))
    ]
    return {"articles": articles, "url": url.replace("&format=json", "&format=html"), "observed_at": observed}


# --------------------------------------------------------------------------- sanctions


def sanctions_index() -> dict[str, Any]:
    """Load EU, UN, OFAC and UK lists into {normalised name: [entries]}."""
    index: dict[str, list[dict[str, str]]] = {}
    status: dict[str, str] = {}

    def add(name: str, list_name: str, ref: str, kind: str) -> None:
        key = norm_name(name)
        if len(key) >= 3:
            index.setdefault(key, []).append({"list": list_name, "name": name, "ref": ref, "kind": kind})

    eu, observed = http(SANCTIONS_LISTS["EU"], ttl=TTL_LIST, binary=True, timeout=180)
    if isinstance(eu, bytes):
        reader = csv.DictReader(io.StringIO(eu.decode("utf-8-sig", "replace")), delimiter=";")
        for row in reader:
            if row.get("NameAlias_WholeName"):
                kind = "person" if row.get("Entity_SubjectType_ClassificationCode") == "person" else "entity"
                add(row["NameAlias_WholeName"], "EU", row.get("Entity_EU_ReferenceNumber", ""), kind)
        status["EU"] = observed
    un, observed = http(SANCTIONS_LISTS["UN"], ttl=TTL_LIST, binary=True, timeout=180)
    if isinstance(un, bytes):
        root = ET.fromstring(un)
        for node in root.iter():
            if node.tag in ("INDIVIDUAL", "ENTITY"):
                ref = node.findtext("REFERENCE_NUMBER", "")
                parts = [node.findtext(t, "") for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME")]
                add(" ".join(p for p in parts if p), "UN", ref, "person" if node.tag == "INDIVIDUAL" else "entity")
                for alias in node.iter():
                    if alias.tag.endswith("_ALIAS") and alias.findtext("ALIAS_NAME"):
                        add(alias.findtext("ALIAS_NAME", ""), "UN", ref, "alias")
        status["UN"] = observed
    for key in ("OFAC_SDN", "OFAC_CONS"):
        data, observed = http(SANCTIONS_LISTS[key], ttl=TTL_LIST, binary=True, timeout=180)
        if isinstance(data, bytes):
            for row in csv.reader(io.StringIO(data.decode("latin-1"))):
                if len(row) > 2:
                    add(row[1], "OFAC", row[0], "person" if row[2].strip() == "individual" else "entity")
            status[key] = observed
    alt, _ = http(SANCTIONS_LISTS["OFAC_ALT"], ttl=TTL_LIST, binary=True, timeout=180)
    if isinstance(alt, bytes):
        for row in csv.reader(io.StringIO(alt.decode("latin-1"))):
            if len(row) > 3:
                add(row[3], "OFAC", row[0], "alias")
    uk, observed = http(SANCTIONS_LISTS["UK"], ttl=TTL_LIST, binary=True, timeout=300)
    if isinstance(uk, bytes):
        lines = uk.decode("utf-8-sig", "replace").splitlines()
        reader = csv.DictReader(lines[1:])  # first line is the report date
        for row in reader:
            full = row.get("Name 6") or " ".join(filter(None, [row.get(f"Name {i}") for i in range(1, 6)]))
            add(full, "UK", row.get("Unique ID", ""), "person" if row.get("Designation Type") == "Individual" else "entity")
        status["UK"] = observed
    return {"index": index, "loaded": status}


def screen(name: str, sanctions: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Exact normalised-name hits and close (token-overlap) candidates."""
    key = norm_name(name)
    exact = list(sanctions["index"].get(key, []))
    tokens = set(key.split())
    close = []
    if len(tokens) >= 2:
        for other, entries in sanctions["index"].items():
            other_tokens = set(other.split())
            if other == key or len(other_tokens) < 2:
                continue
            overlap = len(tokens & other_tokens) / len(tokens | other_tokens)
            if overlap >= 0.75:
                close.extend(entries)
    return {"exact": exact, "close": close[:10]}


def ecb_rates() -> dict[str, float]:
    """Latest ECB reference rates: units of currency per 1 EUR."""
    data, _ = http("https://data-api.ecb.europa.eu/service/data/EXR/D..EUR.SP00.A?lastNObservations=1&format=csvdata", ttl=TTL_LIST)
    rates = {"EUR": 1.0}
    if isinstance(data, str):
        for row in csv.DictReader(io.StringIO(data)):
            try:
                rates[row["CURRENCY"]] = float(row["OBS_VALUE"])
            except (KeyError, ValueError):
                continue
    return rates
