import json

import pytest

from app.backend.profiles import ProfileLoadError, ProfileRegistry, _load_profiles
from app.backend.settings import get_settings


def test_repository_profiles_load_into_memory() -> None:
    registry = ProfileRegistry.load(get_settings().profiles_dir)

    assert registry.buyers
    assert registry.suppliers
    assert registry.get_supplier("supplier-cze-65414128") is not None
    assert registry.get_buyer("buyer-deu-991-19518-88") is not None


def test_supplier_agent_is_opted_out_by_default() -> None:
    registry = ProfileRegistry.load(get_settings().profiles_dir)
    supplier = registry.get_supplier("supplier-cze-65414128")

    assert supplier is not None
    assert supplier.agent_settings is None


def test_duplicate_profile_ids_fail_loading(tmp_path) -> None:
    profiles_path = tmp_path / "buyers.json"
    profile = {
        "id": "buyer-1",
        "role": "demand",
        "name": "Buyer",
        "country": "POL",
    }
    profiles_path.write_text(json.dumps([profile, profile]), encoding="utf-8")

    with pytest.raises(ProfileLoadError, match="Duplicate profile id"):
        _load_profiles(profiles_path, "demand")
