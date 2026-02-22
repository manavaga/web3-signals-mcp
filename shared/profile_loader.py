from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml


def load_profile(profile_path: Path) -> Dict[str, Any]:
    """Load a YAML profile and validate it has required fields."""
    raw = profile_path.read_text(encoding="utf-8")
    profile = yaml.safe_load(raw)
    if not isinstance(profile, dict):
        raise ValueError(f"Profile must be a YAML mapping, got {type(profile)}")
    return profile


def get_assets(profile: Dict[str, Any]) -> List[str]:
    """Extract assets list from profile. Single source of truth."""
    assets = profile.get("assets", [])
    if not assets:
        raise ValueError("Profile must define at least one asset in 'assets' list")
    return [str(a).upper() for a in assets]


def get_threshold(profile: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """
    Safely traverse nested profile keys.
    Example: get_threshold(profile, "thresholds", "long_short_min", default=0.55)
    """
    current = profile
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def is_source_enabled(profile: Dict[str, Any], source_name: str) -> bool:
    """Check if a data source is enabled in the profile."""
    source = profile.get(source_name, {})
    if isinstance(source, dict):
        return bool(source.get("enabled", False))
    return False
