"""Reviewed, HTTPS allowlisted site profiles for public HTML chapter collection.

Profiles describe our own conservative operational budget, NOT a claim that
the host authorizes scraping. robots.txt and site terms remain authoritative;
unsupported scripts, logins, paid chapters and challenges are never bypassed.
Additional sites need a reviewed adapter, fixtures and live metadata gate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSiteProfile:
    name: str
    title: str
    hosts: frozenset[str]
    minimum_delay_seconds: float
    daily_requests: int
    mode: str = "public_html"

    def __post_init__(self) -> None:
        if (not self.name or not self.title or not self.hosts or
            not 1.5 <= self.minimum_delay_seconds <= 30 or
            not 1 <= self.daily_requests <= 10_000 or
            any(not h or h.lower() != h or "*" in h for h in self.hosts)):
            raise ValueError("Invalid allowlisted source-site profile")


SITE_PROFILES: dict[str, SourceSiteProfile] = {
    "royalroad": SourceSiteProfile(
        "royalroad", "Royal Road", frozenset({"www.royalroad.com", "royalroad.com"}),
        minimum_delay_seconds=4.0, daily_requests=240,
    ),
    "webnovel": SourceSiteProfile(
        "webnovel", "WebNovel", frozenset({"www.webnovel.com", "webnovel.com"}),
        minimum_delay_seconds=8.0, daily_requests=100,
    ),
    "uukanshu": SourceSiteProfile(
        "uukanshu", "UUKanshu", frozenset({"uukanshu.cc"}),
        minimum_delay_seconds=6.0, daily_requests=180,
    ),
    "wikisource_en": SourceSiteProfile(
        "wikisource_en", "Wikisource (English)",
        frozenset({"en.wikisource.org"}),
        minimum_delay_seconds=5.0, daily_requests=120,
    ),
    "wikisource_ru": SourceSiteProfile(
        "wikisource_ru", "Викитека (русский)",
        frozenset({"ru.wikisource.org"}),
        minimum_delay_seconds=5.0, daily_requests=120,
    ),
    "ao3": SourceSiteProfile(
        "ao3", "Archive of Our Own (AO3)",
        frozenset({"archiveofourown.org"}),
        minimum_delay_seconds=8.0, daily_requests=80,
    ),
}


def site_profile(source: str) -> SourceSiteProfile:
    """Never select a policy for a caller-supplied arbitrary hostname."""
    try:
        return SITE_PROFILES[source]
    except KeyError as exc:
        raise ValueError(f"Unsupported source: {source}") from exc


def public_site_profiles() -> list[dict]:
    """Show policy, not per-member or aggregate crawl activity."""
    return [
        {
            "id": profile.name, "title": profile.title, "mode": profile.mode,
            "minimum_delay_seconds": profile.minimum_delay_seconds,
            "daily_requests": profile.daily_requests,
            "hosts": sorted(profile.hosts),
            "access": "public_html_subject_to_robots",
        }
        for profile in SITE_PROFILES.values()
    ]
