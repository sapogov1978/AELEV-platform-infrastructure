"""Private model-process values, not alternate public domain contracts."""

import hashlib
import re
from dataclasses import dataclass
from typing import cast

LANGUAGE_PATTERN = r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$"
_LANGUAGE = re.compile(LANGUAGE_PATTERN)


class TranslatorConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ProfileLocalizationRequest:
    translator_ref: str
    source_language: str
    target_language: str
    profile: dict[str, object]


@dataclass(frozen=True)
class EvidenceTranslationRequest:
    translator_ref: str
    source_language: str
    target_language: str
    source_digest: str
    text: str


def _request(payload: object, expected: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    item = cast(dict[str, object], payload)
    if item.get("translator_ref") != expected:
        raise TranslatorConfigurationError("TRANSLATOR_VERSION_MISMATCH")
    if item.get("schema_version") != "1.0.0":
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    for key in ("source_language", "target_language"):
        value = item.get(key)
        if not isinstance(value, str) or not _LANGUAGE.fullmatch(value):
            raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    return item


def parse_profile_localization_request(
    payload: object, *, expected_translator_ref: str
) -> ProfileLocalizationRequest:
    item = _request(payload, expected_translator_ref)
    if set(item) != {
        "schema_version",
        "translator_ref",
        "source_language",
        "target_language",
        "profile",
    }:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    profile = item.get("profile")
    if not isinstance(profile, dict):
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    profile = cast(dict[str, object], profile)
    if set(profile) != {
        "subject_area",
        "positive_terms",
        "excluded_terms",
        "excluded_topics",
    }:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    subject = profile["subject_area"]
    if not isinstance(subject, str) or not 0 < len(subject.strip()) <= 500:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    for key in ("positive_terms", "excluded_terms", "excluded_topics"):
        values = profile[key]
        if not isinstance(values, list):
            raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
        phrases = cast(list[object], values)
        if len(phrases) > 100 or any(
            not isinstance(value, str) or not 0 < len(value.strip()) <= 500
            for value in phrases
        ):
            raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    return ProfileLocalizationRequest(
        expected_translator_ref,
        str(item["source_language"]),
        str(item["target_language"]),
        profile,
    )


def parse_evidence_translation_request(
    payload: object, *, expected_translator_ref: str
) -> EvidenceTranslationRequest:
    item = _request(payload, expected_translator_ref)
    if set(item) != {
        "schema_version",
        "translator_ref",
        "source_language",
        "target_language",
        "source_digest",
        "text",
    }:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    value = item["text"]
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 12000:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    source_digest = "sha256:" + hashlib.sha256(value.strip().encode()).hexdigest()
    if item["source_digest"] != source_digest:
        raise TranslatorConfigurationError("TRANSLATOR_REQUEST_INVALID")
    return EvidenceTranslationRequest(
        expected_translator_ref,
        str(item["source_language"]),
        str(item["target_language"]),
        source_digest,
        value.strip(),
    )
