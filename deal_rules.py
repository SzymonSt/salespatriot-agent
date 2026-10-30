"""Static rule tables for per-deal due diligence (D2, D3, D4, D7, D8, D12).

Legal facts here are [OWN KNOWLEDGE — verify] unless noted; they encode common EU practice so the
deal agent can raise the right questions. They are not legal advice. Keep each table small and
cite the instrument so a reviewer can check it.
"""

from __future__ import annotations

from collections import deque

# --------------------------------------------------------------------------- D2: what the item is

# CPV (most specific prefix wins) → EU Common Military List category, Firearms Directive
# (EU) 2021/555 category when the item can also be a civilian firearm, and ADR transport class.
# ML1 smooth-bore/small arms ≤ 12.7 mm · ML2 > 12.7 mm guns, mortars, artillery · ML3 ammunition
# · ML4 bombs, rockets, grenades, mines, torpedoes · Firearms Directive cat A = prohibited
# for civilians (automatic weapons), B = authorisation, C = declaration.
CPV_RULES: list[tuple[str, dict[str, str]]] = [
    ("35311", {"ml": "not on the Military List (edged weapons)", "civil": "national law", "adr": "not dangerous goods"}),
    ("35312", {"ml": "ML7/ML1 depending on agent (gas guns)", "civil": "B", "adr": "check agent (Class 2/6.1)"}),
    ("353213", {"ml": "ML1", "civil": "A", "adr": "not dangerous goods"}),        # machine guns
    ("353211", {"ml": "ML1", "civil": "B", "adr": "not dangerous goods"}),        # handguns
    ("353212", {"ml": "ML1", "civil": "B or C", "adr": "not dangerous goods"}),   # rifles
    ("35321", {"ml": "ML1", "civil": "B", "adr": "not dangerous goods"}),
    ("35322", {"ml": "ML2", "civil": "military only", "adr": "not dangerous goods"}),
    ("353311", {"ml": "ML3", "civil": "ammunition for cat B/C firearms", "adr": "Class 1 — small-arms cartridges usually UN0012 (1.4S)"}),
    ("353315", {"ml": "ML3", "civil": "ammunition for cat B/C firearms", "adr": "Class 1 — small-arms cartridges usually UN0012 (1.4S)"}),
    ("353312", {"ml": "ML3/ML4", "civil": "military only", "adr": "Class 1 — shells, usually 1.1/1.2 (high hazard)"}),
    ("353313", {"ml": "ML4", "civil": "military only", "adr": "Class 1 — grenades, usually 1.1/1.2"}),
    ("353314", {"ml": "ML4", "civil": "military only", "adr": "Class 1 — mines, usually 1.1"}),
    ("35332", {"ml": "ML4", "civil": "military only", "adr": "Class 1 — naval munitions, usually 1.1"}),
    ("35333", {"ml": "ML4", "civil": "military only", "adr": "Class 1 — bombs/rockets, usually 1.1"}),
    ("35331", {"ml": "ML3", "civil": "depends on calibre", "adr": "Class 1 — confirm UN number with the supplier"}),
    ("35330", {"ml": "ML3", "civil": "depends on calibre", "adr": "Class 1 — confirm UN number with the supplier"}),
    ("35341", {"ml": "ML1 components", "civil": "essential components follow the firearm", "adr": "not dangerous goods"}),
    ("35342", {"ml": "ML4 components", "civil": "military only", "adr": "not dangerous goods (unless energetic parts)"}),
    ("35343", {"ml": "ML2 components", "civil": "military only", "adr": "not dangerous goods"}),
    ("35320", {"ml": "ML1/ML2", "civil": "depends on type", "adr": "not dangerous goods"}),
    ("3534", {"ml": "ML1–ML4 components", "civil": "depends on part", "adr": "not dangerous goods"}),
    ("3533", {"ml": "ML3/ML4", "civil": "depends on calibre", "adr": "Class 1 — confirm UN number with the supplier"}),
    ("353", {"ml": "ML1–ML4 (confirm from specification)", "civil": "depends on item", "adr": "confirm with the supplier"}),
]

# Words in a tender title/description that pin down a heavier category than the CPV says.
HEAVY_ITEM_WORDS = {
    "ML2": ["mortar", "moździerz", "mortier", "mortero", "howitzer", "haubic", "obús", "obus", "155 mm", "120 mm", "105 mm", "81 mm", "60 mm"],
    "ML4": ["bomb", "bomba", "bombe", "rocket", "rakiet", "roquette", "cohete", "grenade", "granat", "granada", "mine", "torped", "blu-", "gbu-"],
}


def cpv_rule(codes: list[str]) -> dict[str, str]:
    best: tuple[int, dict[str, str]] | None = None
    for code in codes:
        for prefix, rule in CPV_RULES:
            if code.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), rule)
    return dict(best[1]) if best else {"ml": "not a weapons CPV", "civil": "n/a", "adr": "n/a"}


# Buyers that may acquire military items: armed forces, police, other state security bodies.
STATE_SECURITY_ACTIVITIES = {"defence", "pub-os", "public-order-and-safety"}
STATE_SECURITY_WORDS = [
    "defen", "obrany", "obrony", "obrambo", "verteidigung", "bundeswehr", "armée", "armada", "army", "ejército",
    "militar", "wojsk", "polic", "polizei", "policja", "gendarm", "жандармерия", "отбраната", "guardia civil",
    "interior", "intérieur", "vnitra", "vidaus", "inrikes", "mswia", "douane", "customs", "border", "granic",
]

# --------------------------------------------------------------------------- embargoes, geography

# EU arms embargoes (Council decisions under CFSP; check the EU Sanctions Map before relying on it).
# Some are partial (non-state actors only); treat any as "escalate" for routing and ownership.
EU_ARMS_EMBARGO = {
    "RUS": "Russia", "BLR": "Belarus", "IRN": "Iran", "PRK": "North Korea", "SYR": "Syria", "MMR": "Myanmar",
    "CHN": "China (1989 embargo)", "VEN": "Venezuela", "LBY": "Libya", "SOM": "Somalia", "SSD": "South Sudan",
    "SDN": "Sudan", "ZWE": "Zimbabwe", "CAF": "Central African Republic", "COD": "DR Congo (non-state)",
    "IRQ": "Iraq (non-state)", "LBN": "Lebanon (non-state)", "YEM": "Yemen (named persons)", "AFG": "Afghanistan (Taliban)",
    "HTI": "Haiti (named persons)",
}

EU = {"AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN", "IRL", "ITA", "LVA",
      "LTU", "LUX", "MLT", "NLD", "POL", "PRT", "ROU", "SVK", "SVN", "ESP", "SWE"}
EEA_EFTA = {"NOR", "ISL", "LIE"}
# SAFE (Reg. (EU) 2025/1106) [VERIFIED 2026-10-30 in knowledge base]: contractors established in
# EU, EEA-EFTA or Ukraine; component cost from outside those ≤ 35%.
SAFE_ELIGIBLE = EU | EEA_EFTA | {"UKR"}
NATO_EUROPE_PLUS = EU | EEA_EFTA | {"GBR", "CHE", "USA", "CAN", "TUR", "ALB", "MNE", "MKD", "UKR"}

# Land borders between European countries (Russia and Belarus left out: no routing through
# embargoed states). GBR–FRA via the Channel Tunnel, which does not take most Class 1 goods.
LAND_BORDERS = {
    "PRT": ["ESP"], "ESP": ["PRT", "FRA"], "FRA": ["ESP", "BEL", "LUX", "DEU", "CHE", "ITA", "GBR"],
    "BEL": ["FRA", "LUX", "DEU", "NLD"], "NLD": ["BEL", "DEU"], "LUX": ["BEL", "DEU", "FRA"],
    "DEU": ["DNK", "POL", "CZE", "AUT", "CHE", "FRA", "LUX", "BEL", "NLD"], "DNK": ["DEU", "SWE"],
    "SWE": ["DNK", "NOR", "FIN"], "NOR": ["SWE", "FIN"], "FIN": ["SWE", "NOR"],
    "EST": ["LVA"], "LVA": ["EST", "LTU"], "LTU": ["LVA", "POL"],
    "POL": ["DEU", "CZE", "SVK", "UKR", "LTU"], "CZE": ["DEU", "POL", "SVK", "AUT"],
    "SVK": ["CZE", "POL", "UKR", "HUN", "AUT"], "AUT": ["DEU", "CZE", "SVK", "HUN", "SVN", "ITA", "CHE", "LIE"],
    "HUN": ["AUT", "SVK", "UKR", "ROU", "SRB", "HRV", "SVN"], "SVN": ["ITA", "AUT", "HUN", "HRV"],
    "HRV": ["SVN", "HUN", "SRB", "BIH", "MNE"], "ITA": ["FRA", "CHE", "AUT", "SVN"],
    "CHE": ["FRA", "DEU", "AUT", "ITA", "LIE"], "LIE": ["CHE", "AUT"],
    "ROU": ["HUN", "UKR", "MDA", "BGR", "SRB"], "BGR": ["ROU", "SRB", "MKD", "GRC", "TUR"],
    "GRC": ["ALB", "MKD", "BGR", "TUR"], "SRB": ["HUN", "ROU", "BGR", "MKD", "XKX", "MNE", "BIH", "HRV"],
    "BIH": ["HRV", "SRB", "MNE"], "MNE": ["HRV", "BIH", "SRB", "XKX", "ALB"], "ALB": ["MNE", "XKX", "MKD", "GRC"],
    "MKD": ["SRB", "XKX", "BGR", "GRC", "ALB"], "XKX": ["SRB", "MNE", "ALB", "MKD"],
    "UKR": ["POL", "SVK", "HUN", "ROU", "MDA"], "MDA": ["ROU", "UKR"], "TUR": ["GRC", "BGR"],
    "GBR": ["FRA", "IRL"], "IRL": ["GBR"],
}
SEA_ONLY = {"CYP", "MLT", "ISL", "ZAF", "USA", "CAN"}


def land_route(origin: str, destination: str) -> list[str] | None:
    """Shortest land route (list of countries incl. both ends), None if sea/air is needed."""
    if origin == destination:
        return [origin]
    if origin not in LAND_BORDERS or destination not in LAND_BORDERS:
        return None
    previous = {origin: None}
    queue = deque([origin])
    while queue:
        country = queue.popleft()
        for neighbour in LAND_BORDERS.get(country, []):
            if neighbour not in previous:
                previous[neighbour] = country
                if neighbour == destination:
                    path = [destination]
                    while previous[path[-1]]:
                        path.append(previous[path[-1]])
                    return path[::-1]
                queue.append(neighbour)
    return None


# --------------------------------------------------------------------------- D4: data rules

# The challenge forbids ITAR/EAR-controlled, classified or CUI content on the platform.
EXPORT_CONTROL_WORDS = [
    "itar", "usml", "ear99", "export administration regulations", "foreign military sales", " fms ",
    "us origin", "u.s. origin", "made in usa", "blu-", "gbu-", "agm-", "aim-", "mk 82", "mk 83", "mk 84", "m855", "m80 ",
]
CLASSIFIED_WORDS = [
    "classified", "nato restricted", "nato confidential", "nato secret", "eu restricted", "eu confidential",
    "vs-nfd", "verschlusssache*", "zastrzeżon*", "poufn*", "tajn*", "niejawn*", "utajen*", "vyhrazen*", "důvěrn*",
    "diffusion restreinte", "confidentiel défense", "secret défense", "clasificad*", "difusión limitada",
    "riservat*", "controlled unclassified", "cui",
]

def find_terms(terms: list[str], text: str) -> list[str]:
    """Whole-word, case-insensitive matches ("itar" must not match "military")."""
    import re

    lowered = f" {text.lower()} "
    hits = set()
    for term in terms:
        word = term.strip()
        prefix = word.endswith("*") or word.endswith("-")  # "poufn*" = stem; "blu-" = designation prefix
        word = word.rstrip("*")
        pattern = r"(?<![\w])" + re.escape(word) + (r"" if prefix else r"(?![\w])")
        # "ITAR-free" / "non-ITAR" are selling points, not risks.
        for match in re.finditer(pattern, lowered):
            around = lowered[max(0, match.start() - 4):match.end() + 6]
            if "non-" + word in around or word + "-free" in around or word + " free" in around:
                continue
            hits.add(word)
    return sorted(hits)


# --------------------------------------------------------------------------- thresholds

# Approximate EU procurement thresholds for supplies (verify the current Commission regulation).
THRESHOLD_SUPPLIES_EUR = {"32009L0081": 443_000, "32014L0024": 143_000}
