from dataclasses import dataclass
import re
from typing import Any

from .models import Candidate


@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    reasons: tuple[str, ...]


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _targets(profile: dict[str, Any]) -> dict[str, Any]:
    targets = profile.get("targets", {})
    if not isinstance(targets, dict):
        raise ValueError("targets must be an object")
    titles = targets.get("titles", [])
    if not isinstance(titles, list) or any(not isinstance(value, str) for value in titles):
        raise ValueError("targets.titles must be a list of strings")
    for name in ("remote_only", "contract_only"):
        if name in targets and not isinstance(targets[name], bool):
            raise ValueError(f"targets.{name} must be boolean")
    engagement_types = targets.get("engagement_types_allowed", ["1099", "C2C"])
    if (
        not isinstance(engagement_types, list)
        or not engagement_types
        or any(not isinstance(value, str) for value in engagement_types)
    ):
        raise ValueError("targets.engagement_types_allowed must be a non-empty list of strings")
    normalized_engagements = {value.upper() for value in engagement_types}
    if not normalized_engagements.issubset(
        {"1099", "C2C", "CONTRACT TYPE UNSPECIFIED OR NEGOTIABLE"}
    ):
        raise ValueError("targets.engagement_types_allowed contains an unsupported value")
    return targets


def title_matches(title: str, profile: dict[str, Any]) -> bool:
    targets = _targets(profile)
    normalized_title = _normalized(title)
    target_titles = [_normalized(value) for value in targets.get("titles", [])]
    return not target_titles or any(
        value in normalized_title or normalized_title in value for value in target_titles
    )


def prefilter(candidate: Candidate, profile: dict[str, Any]) -> FilterDecision:
    targets = _targets(profile)
    reasons: list[str] = []

    title = _normalized(candidate.title)
    if not title_matches(candidate.title, profile):
        return FilterDecision(False, ("title",))

    if targets.get("remote_only"):
        location_says_remote = "remote" in _normalized(candidate.location)
        if candidate.remote is False:
            return FilterDecision(False, ("remote",))
        if candidate.remote is None and not location_says_remote:
            reasons.append("remote_unknown")

    if targets.get("contract_only"):
        description = _normalized(candidate.description)
        engagement_text = f"{title} {description}"
        c2c_allowed = bool(
            re.search(
                r"\b(?:c2c|corp to corp)\s+(?:(?:is|are)\s+)?(?:allowed|accepted|available|okay|ok|welcome)\b",
                engagement_text,
            )
        )
        contractor_1099_allowed = bool(
            re.search(
                r"\b1099\s+(?:(?:is|are)\s+)?(?:allowed|accepted|available|okay|ok|welcome)\b",
                engagement_text,
            )
        )
        w2_only = bool(
            re.search(
                r"\b(?:w\s*2\s+(?:candidates?\s+)?only|only\s+w\s*2(?:\s+candidates?)?)\b",
                engagement_text,
            )
        )
        contractor_excluded = bool(
            re.search(
                r"\b(?:no\s+contractors?|contractors?\s+(?:(?:is|are)\s+)?not\s+(?:allowed|accepted|welcome))\b",
                engagement_text,
            )
        )
        c2c_prohibited = bool(
            re.search(
                r"\bno (?:c2c|corp to corp)\b|\b(?:c2c|corp to corp)\s+(?:(?:is|are)\s+)?not\s+(?:allowed|accepted|available|okay|ok|welcome)\b",
                engagement_text,
            )
        )
        contractor_1099_prohibited = bool(
            re.search(
                r"\bno 1099\b|\bno (?:c2c|corp to corp)\s+(?:or|and)\s+1099\b|\b1099\s+(?:(?:is|are)\s+)?not\s+(?:allowed|accepted|available|okay|ok|welcome)\b",
                engagement_text,
            )
        )
        allowed_engagements = {
            value.upper()
            for value in targets.get("engagement_types_allowed", ["1099", "C2C"])
        }
        affirmative_engagements = {
            name
            for name, present in (("C2C", c2c_allowed), ("1099", contractor_1099_allowed))
            if present
        }
        prohibited_engagements = {
            name
            for name, present in (("C2C", c2c_prohibited), ("1099", contractor_1099_prohibited))
            if present
        }
        if (
            w2_only
            or contractor_excluded
            or (
                {"C2C", "1099"} & allowed_engagements
                and {"C2C", "1099"} & allowed_engagements
                <= prohibited_engagements
            )
            or (
                affirmative_engagements
                and not affirmative_engagements & allowed_engagements
            )
        ):
            return FilterDecision(False, ("engagement",))
        employment = _normalized(candidate.employment_type)
        signal_description = re.sub(
            r"\b(?:not(?: a)?|non|no) contract(?:or)?\b", "", description
        )
        contract_signal = bool(
            re.search(
                r"\bcontract(?:or)?\b|\bc2c\b|\bcorp to corp\b|\b1099\b",
                signal_description,
            )
            or employment in {"contract", "contractor", "temporary"}
        )
        known_non_contract = {
            "full time",
            "permanent",
            "intern",
            "internship",
            "part time",
        }
        if employment in known_non_contract and not contract_signal:
            return FilterDecision(False, ("contract",))
        if not employment and not contract_signal:
            reasons.append("contract_unknown")

    return FilterDecision(True, tuple(reasons))
