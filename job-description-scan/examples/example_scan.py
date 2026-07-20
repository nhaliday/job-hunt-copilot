"""Sanitized scan-config template.

Copy into your (private) content project as ``scans/<company>.py`` and edit.
The engine imports this module by dotted path from the content project's cwd
(``--scan scans.<company>``); nothing case-specific lives in the engine.
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from job_description_scan.config import BoardSource, Ladder, RankConfig, Scan

Role = Literal["swe", "pre_sales_se", "post_sales_se", "nontechnical", "other"]
Level = Literal[
    "entry",
    "engineer",
    "senior",
    "staff",
    "principal",
    "distinguished",
    "unknown",
]
ICOrLead = Literal["ic", "lead", "supervisor", "unknown"]
FitTier = Literal["strong", "stretch", "long_shot", "blocked"]


class Extraction(BaseModel):
    """JD-only facts; always populated. Field descriptions flow into the JSON
    schema sent to the LLM — use them to steer extraction."""

    role: Role = Field(
        description=(
            "Role family. Choose carefully — title alone is not enough; read "
            "the JD body.\n\n"
            "- swe: software engineer who writes production code as their "
            "primary job.\n"
            "- pre_sales_se: TECHNICAL pre-sales role — designs solutions, "
            "runs demos, writes proof-of-concept code, owns technical "
            "evaluation during the sales cycle. Must require hands-on "
            "technical work; NOT pure selling.\n"
            "- post_sales_se: TECHNICAL post-sales role — embedded with "
            "customers after the sale to implement, troubleshoot, escalate "
            "(e.g. Forward Deployed Engineer, Resident Engineer).\n"
            "- nontechnical: revenue-carrying or business-function role with "
            "no required production coding or technical solutioning.\n"
            "- other: anything else (data scientist, PM, designer, ...)."
        )
    )
    level: Level = Field(
        description=(
            "Career level. Use the leveling framework provided in context "
            "(see system_context_files): entry (new grad), engineer "
            "(mid-career IC, ~3-7 yrs), senior, staff, principal, "
            "distinguished. Use 'unknown' if the JD does not map cleanly."
        )
    )
    ic_or_lead: ICOrLead = Field(
        description="Individual contributor, formal lead/manager, or supervisor."
    )
    yoe_min: int | None = Field(
        description=(
            "Minimum years of experience stated in the JD. Null if not "
            "specified."
        )
    )
    type_of_experience: str = Field(
        description=(
            "Brief phrase describing the kind of experience required "
            "(e.g. 'distributed systems', 'customer-facing data engineering')."
        )
    )
    required_quals: list[str] = Field(
        description="Quals stated as required/must-have/minimum."
    )
    desired_quals: list[str] = Field(
        description="Quals stated as preferred/nice-to-have/bonus."
    )


class Comparison(BaseModel):
    """Fit/gap fields; populated only when --resume is provided."""

    missing_required_quals: list[str] = Field(
        description="Items from required_quals not satisfied by the resume."
    )
    yoe_gap_years: int = Field(
        description=(
            "Years-of-experience gap. Positive if the candidate is short of "
            "the minimum; negative if the candidate exceeds it; zero if exact."
        )
    )
    tier_reasoning: str = Field(
        description=(
            "One or two sentences justifying the fit_tier you are about to "
            "choose. State the dominant factor. Reason explicitly before "
            "picking the tier."
        )
    )
    fit_tier: FitTier = Field(description="Overall fit assessment.")


# Optional deterministic pre-filter on Posting.location (skipped postings cost
# nothing). Match structured metadata only — never filter on title.
# Workday caveat: single-location list rows are bare "City, ST" with no country,
# so a country-anchored regex like this one matches nothing there — write
# Workday filters against city/state/"Remote" forms (see CLAUDE.md).
US_LOCATION = re.compile(r"\b(USA?|United States)\b", re.IGNORECASE)


scan = Scan(
    # kind: greenhouse | ashby | lever | workday | smartrecruiters.
    # Slugs: workday is "hostprefix/site" (e.g. "acme.wd5/Acme_Careers");
    # smartrecruiters is the API company identifier (may differ from the
    # careers-site slug). See CLAUDE.md "Adding a new scan".
    source=BoardSource(kind="greenhouse", slug="acme"),
    extraction=Extraction,
    comparison=Comparison,
    # Reference docs inlined into the cached system prompt. This example
    # expects the file at the content project root (one level above scans/).
    system_context_files=[
        Path(__file__).parent.parent / "Levels.fyi Standard SWE Level Framework.md",
    ],
    model="claude-haiku-4-5",
    location_filter=US_LOCATION,
)


# Optional pairwise-ranking config (see job_description_scan/ranking.py).
# One ladder per role family — families are not comparable head-to-head.
_EXCLUDE = re.compile(r"new grad|internship", re.IGNORECASE)

ranking = RankConfig(
    ladders=[
        Ladder(
            roles=("swe",),
            label="software engineering",
            exclude_title=_EXCLUDE,
        ),
        Ladder(
            roles=("post_sales_se",),
            label="forward-deployed / customer-embedded engineering",
            exclude_title=_EXCLUDE,
        ),
    ]
)
