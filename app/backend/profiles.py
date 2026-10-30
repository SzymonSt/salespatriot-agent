import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SupplierAgentSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    opted_in_categories: list[str] = Field(default_factory=list)
    capacity_declarations: list[str] = Field(default_factory=list)


class BuyerAgentPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["unconfigured", "draft", "active"] = "unconfigured"
    version: str | int | None = None
    permitted_actions: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)


class CompanyProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    role: Literal["demand", "supply"]
    name: str
    country: str
    identifier: str = ""
    agent_settings: SupplierAgentSettings | None = None
    agent_policy: BuyerAgentPolicy | None = None


class ProfileLoadError(ValueError):
    pass


def _load_profiles(path: Path, expected_role: Literal["demand", "supply"]) -> dict[str, CompanyProfile]:
    try:
        raw_profiles = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileLoadError(f"Could not load profile file {path}: {error}") from error

    if not isinstance(raw_profiles, list):
        raise ProfileLoadError(f"Profile file {path} must contain a JSON array")

    profiles: dict[str, CompanyProfile] = {}
    for index, raw_profile in enumerate(raw_profiles):
        try:
            profile = CompanyProfile.model_validate(raw_profile)
        except ValidationError as error:
            raise ProfileLoadError(
                f"Invalid profile at {path}, array index {index}: {error}"
            ) from error
        if profile.role != expected_role:
            raise ProfileLoadError(
                f"Profile {profile.id} in {path} has role {profile.role!r}; "
                f"expected {expected_role!r}"
            )
        if profile.id in profiles:
            raise ProfileLoadError(f"Duplicate profile id {profile.id!r} in {path}")
        profiles[profile.id] = profile
    return profiles


class ProfileRegistry:
    def __init__(
        self,
        buyers: dict[str, CompanyProfile],
        suppliers: dict[str, CompanyProfile],
    ) -> None:
        self.buyers = buyers
        self.suppliers = suppliers

    @classmethod
    def load(cls, profiles_dir: Path) -> "ProfileRegistry":
        buyers = _load_profiles(profiles_dir / "buyers.json", "demand")
        suppliers = _load_profiles(profiles_dir / "suppliers.json", "supply")
        return cls(buyers=buyers, suppliers=suppliers)

    def get_buyer(self, profile_id: str) -> CompanyProfile | None:
        return self.buyers.get(profile_id)

    def get_supplier(self, profile_id: str) -> CompanyProfile | None:
        return self.suppliers.get(profile_id)
