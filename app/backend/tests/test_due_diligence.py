import json

import pytest

from app.backend.due_diligence import CACHE_DIR, REPO_ROOT, initial_due_diligence

SUPPLIERS = json.loads((REPO_ROOT / "data" / "profiles" / "suppliers.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("profile", SUPPLIERS, ids=[p["id"] for p in SUPPLIERS])
def test_stored_score_matches_what_agents_see(profile) -> None:
    result = initial_due_diligence(profile)

    assert result.source == "stored"
    assert result.score == profile["due_diligence"]["total_score"]
    assert result.verdict == profile["due_diligence"]["verdict"]
    assert {c.id for c in result.kyc_checks} == set(profile["kyc"]["checks"])
    assert all(check.id and check.status for check in result.kyc_checks)


@pytest.mark.skipif(not CACHE_DIR.exists(), reason="needs the due-diligence source cache (.cache/dd)")
def test_offline_recompute_is_close_to_stored_score() -> None:
    gaps = {
        p["id"]: abs(initial_due_diligence(p, recompute=True).score - p["due_diligence"]["total_score"])
        for p in SUPPLIERS
    }

    # Identical for almost all suppliers; small drift only where data changed since scoring.
    assert max(gaps.values()) <= 0.5
    assert sum(gap == 0 for gap in gaps.values()) >= len(SUPPLIERS) - 2


def test_failed_gate_blocks_and_open_hard_kyc_needs_documents() -> None:
    profile = dict(SUPPLIERS[0])
    dd = dict(profile["due_diligence"])
    dd["gates"] = [{"check": "No sanctions match (EU, UN, OFAC, UK)", "status": "fail", "detail": "Name match"}]
    assert initial_due_diligence({**profile, "due_diligence": dd}).decision == "BLOCKED"

    dd["gates"] = [{"check": "Company active", "status": "pass", "detail": ""}]
    kyc = {"kyc_status": "limited", "open_items": ["K7: needs_upload"],
           "checks": {"K7": {"title": "Licence", "gate": "HARD", "status": "needs_upload", "summary": ""}}}
    assert initial_due_diligence({**profile, "due_diligence": dd, "kyc": kyc}).decision == "NEEDS_DOCUMENTS"

    kyc["checks"]["K7"]["status"] = "pass"
    assert initial_due_diligence({**profile, "due_diligence": dd, "kyc": kyc}).decision == "CLEARED"
