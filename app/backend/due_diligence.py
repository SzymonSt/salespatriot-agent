"""Initial (pre-deal) due-diligence score of a supplier.

The score comes from the same pipeline the agents see (`supplier_due_diligence.py`,
method dd-v1): six weighted categories mapped to 1-10, hard gates, confidence caps and
the K1-K11 KYC checks. `initial_due_diligence` returns the stored result written into
the supplier profile by that pipeline, or recomputes it offline from the pipeline's
source cache (`.cache/dd`) when the profile has none or `recompute=True`. Both paths use
the pipeline's own scoring code, so the numbers match what the agents and the UI show.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "data" / "supplier_evidence"
CACHE_DIR = REPO_ROOT / ".cache" / "dd"


class DueDiligenceCheck(BaseModel):
    """One gate or KYC check, in the {id, status, evidence} shape the buyer agent uses."""

    id: str
    title: str
    gate: str
    status: str
    evidence: str


class CategoryScore(BaseModel):
    id: str
    label: str
    weight: float
    score: float | None
    reasoning: str


class InitialDueDiligence(BaseModel):
    supplier_id: str
    supplier_name: str
    score: float
    verdict: str
    confidence: str
    data_coverage: float | None
    method_version: str
    assessed_at: str
    source: Literal["stored", "recomputed_offline"]
    decision: Literal["BLOCKED", "NEEDS_DOCUMENTS", "CLEARED"]
    gates: list[DueDiligenceCheck]
    categories: list[CategoryScore]
    red_flags: list[str]
    kyc_status: str | None
    kyc_checks: list[DueDiligenceCheck]
    open_items: list[str]


def _pipeline():
    """Import the scoring pipeline from the repository root, offline only."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import dd_sources
    import supplier_due_diligence

    dd_sources.OFFLINE = True
    dd_sources.CACHE_DIR = CACHE_DIR
    return supplier_due_diligence, dd_sources


@lru_cache(maxsize=1)
def _offline_inputs() -> tuple[dict[str, Any], dict[str, float]]:
    """Sanctions lists and ECB rates from the cache; loaded once (a few seconds)."""
    _, src = _pipeline()
    return src.sanctions_index(), src.ecb_rates()


def _recompute(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    sdd, _ = _pipeline()
    sanctions, rates = _offline_inputs()
    data = sdd.collect(profile, sanctions, with_media=True)
    documents = sdd.load_documents(EVIDENCE_DIR, profile["id"])
    dd = sdd.score_supplier(profile, data, documents, rates)
    return dd, sdd.build_kyc(profile, data, dd, documents)


def _decision(gates: list[DueDiligenceCheck], kyc: Mapping[str, Any] | None) -> str:
    if any(g.status == "fail" for g in gates) or (kyc or {}).get("kyc_status") == "blocked":
        return "BLOCKED"
    hard_open = [
        check for check in ((kyc or {}).get("checks") or {}).values()
        if check.get("gate", "").startswith("HARD") and check.get("status") != "pass"
    ]
    return "NEEDS_DOCUMENTS" if hard_open else "CLEARED"


def initial_due_diligence(profile: Mapping[str, Any], *, recompute: bool = False) -> InitialDueDiligence:
    """Return the supplier's initial due-diligence score and checks.

    `profile` is a supplier profile from data/profiles/suppliers.json (a dict, or a
    CompanyProfile's model_dump()). With `recompute=False` the stored pipeline result is
    used when present; otherwise the pipeline scores the profile from its offline cache.
    """
    profile = dict(profile)
    stored = profile.get("due_diligence")
    if stored and not recompute:
        dd, kyc, source = stored, profile.get("kyc"), "stored"
    else:
        dd, kyc = _recompute(profile)
        source = "recomputed_offline"

    gates = [
        DueDiligenceCheck(
            id=f"G{index}",
            title=gate["check"],
            gate="HARD",
            status=gate["status"],
            evidence=gate.get("detail", ""),
        )
        for index, gate in enumerate(dd.get("gates") or [], start=1)
    ]
    kyc_checks = [
        DueDiligenceCheck(
            id=code,
            title=check.get("title", ""),
            gate=check.get("gate", ""),
            status=check.get("status", ""),
            evidence=check.get("summary", ""),
        )
        for code, check in ((kyc or {}).get("checks") or {}).items()
    ]
    return InitialDueDiligence(
        supplier_id=profile["id"],
        supplier_name=profile.get("name", ""),
        score=dd["total_score"],
        verdict=dd["verdict"],
        confidence=dd["confidence"],
        data_coverage=dd.get("data_coverage"),
        method_version=dd.get("method_version", ""),
        assessed_at=dd.get("assessed_at", ""),
        source=source,
        decision=_decision(gates, kyc),
        gates=gates,
        categories=[
            CategoryScore(
                id=key,
                label=category.get("label", key),
                weight=category.get("weight", 0.0),
                score=category.get("score"),
                reasoning=category.get("reasoning", ""),
            )
            for key, category in (dd.get("categories") or {}).items()
        ],
        red_flags=list(dd.get("red_flags") or []),
        kyc_status=(kyc or {}).get("kyc_status"),
        kyc_checks=kyc_checks,
        open_items=list((kyc or {}).get("open_items") or []),
    )


if __name__ == "__main__":
    # Run with the backend environment (Python 3.11 + pydantic), from the repository root:
    #   uv run --project app/backend python -m app.backend.due_diligence supplier-pol-9482182612 [--recompute]
    profiles_path = REPO_ROOT / "data" / "profiles" / "suppliers.json"
    by_id = {p["id"]: p for p in json.loads(profiles_path.read_text(encoding="utf-8"))}
    result = initial_due_diligence(by_id[sys.argv[1]], recompute="--recompute" in sys.argv)
    print(result.model_dump_json(indent=2))
