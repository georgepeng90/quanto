"""Pure deterministic filtering of submitted facts, without network or file access."""
from collections import Counter
from datetime import datetime, time
from zoneinfo import ZoneInfo

from quanto.framework import canonical_hash
from quanto.objects import digest
from quanto.screening.contracts import CandidateResult, EvidenceDigest, ScreeningRequest, ScreeningResult

SHANGHAI = ZoneInfo("Asia/Shanghai")


def session_close(day):
    return datetime.combine(day, time(15), SHANGHAI)


def screen(request: ScreeningRequest) -> ScreeningResult:
    available = {}
    for source in request.evidence:
        published = source.published_at
        if published is not None and not isinstance(published, datetime):
            following = next((day for day in request.trading_days if day > published), None)
            published = session_close(following) if following else None
        available[source.id] = max(published, source.retrieved_at) if published else None
    usable = {key for key, stamp in available.items() if stamp and stamp <= request.as_of}
    superseded = {source.revises for source in request.evidence if source.id in usable and source.revises}
    completed = [day for day in request.trading_days if session_close(day) <= request.as_of]
    latest = completed[-1] if completed else None

    def citation_reasons(citations, field):
        if not citations:
            return [f"missing_citation:{field}"]
        return [reason for citation in citations for reason in (
            ([f"unavailable_evidence:{citation.evidence_id}"] if citation.evidence_id not in usable else []) +
            ([f"superseded_evidence:{citation.evidence_id}"] if citation.evidence_id in superseded else []))]

    outcomes = []
    eligible = []
    scope_reasons = citation_reasons((request.universe_citation,), "universe") if request.universe_citation else []
    if request.trading_days and request.trading_days[-1] < request.as_of.astimezone(SHANGHAI).date():
        scope_reasons.append("incomplete_calendar")
    for index, candidate in enumerate(request.candidates):
        reasons = list(scope_reasons)
        identity = candidate.instrument_id
        if identity.instrument_type != "stock" or identity.exchange not in {"SSE", "SZSE"}:
            reasons.append("unsupported_instrument")
        if candidate.board not in request.rules.allowed_boards:
            reasons.append("unsupported_board")
        if not latest:
            reasons.append("missing_calendar")
        for field in ("name", "board", "industry", "listing_date", "status", "business_summary"):
            if not getattr(candidate, field):
                reasons.append(f"missing_{field}")
            else:
                reasons.extend(citation_reasons(candidate.citations.get(field, ()), field))
        if candidate.listing_date and (request.as_of.astimezone(SHANGHAI).date() - candidate.listing_date).days < request.rules.min_listing_days:
            reasons.append("listing_too_recent")
        if candidate.status and candidate.status != "trading":
            reasons.append("unsupported_status")
        if candidate.status in request.rules.excluded_statuses:
            reasons.append("excluded_status")
        if candidate.status_date != latest or candidate.status_date is None:
            reasons.append("stale_status")
        bars = [bar for bar in candidate.daily if bar.date in completed]
        valid = []
        for bar in bars:
            problems = citation_reasons(bar.citations, f"daily:{bar.date}")
            if bar.amount_unit != "CNY":
                problems.append("unknown_amount_unit")
            reasons.extend(problems)
            if not problems:
                valid.append(bar)
        if len(valid) < request.rules.min_history_days:
            reasons.append("insufficient_history")
        window_days = completed[-request.rules.liquidity_window:]
        window = [bar for bar in valid if bar.date in window_days]
        average = None
        if len(window) < request.rules.liquidity_window:
            reasons.append("insufficient_liquidity_window")
        else:
            average = sum(bar.amount for bar in window) / len(window)
            if average < request.rules.min_average_amount:
                reasons.append("liquidity_below_minimum")
        if not candidate.reasons or not candidate.risks or not candidate.business_summary.strip():
            reasons.append("incomplete_research")
        if candidate.recommendation != "include":
            reasons.append("agent_" + candidate.recommendation)
        if not reasons:
            eligible.append(index)
        outcomes.append(CandidateResult(instrument_id=identity,
            decision="not_researched" if candidate.recommendation == "not_researched" else "excluded",
            reasons=tuple(dict.fromkeys(reasons)), valid_history_days=len(valid), average_amount=average))
    industries = Counter()
    members = []
    for index in sorted(eligible, key=lambda i: (request.candidates[i].rank, i)):
        candidate = request.candidates[index]
        reason = ("industry_limit" if industries[candidate.industry] >= request.rules.industry_max else
                  "target_limit" if len(members) >= request.rules.target_count else None)
        outcomes[index] = outcomes[index].model_copy(update={"decision": "excluded" if reason else "selected",
                                                           "reasons": (reason,) if reason else ()})
        if not reason:
            members.append(candidate.instrument_id)
            industries[candidate.industry] += 1
    return ScreeningResult(input_hash=canonical_hash(request.model_dump(mode="json", exclude={"idempotency_key"})),
        members=tuple(members), candidates=tuple(outcomes), shortfall=request.rules.target_count - len(members),
        evidence=tuple(EvidenceDigest(id=source.id, content_hash=digest(source.text.encode("utf-8")),
            archive_level=source.archive_level, available_at=available[source.id]) for source in request.evidence))
