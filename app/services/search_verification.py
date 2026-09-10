"""One verification policy for foreground, continuation and prefetch."""

from app.config import settings
from app.services.search_budget import BudgetExhausted, RequestTimeout
from app.services.search_reranker import (
    RERANK_PROMPT_VERSION,
    evidence_from_scored,
    judge_candidate_evidence,
    _visual_candidates,
    merge_visual_decisions,
    visual_trigger_reason,
)
from app.services.search_visual_verifier import judge_visual_candidates


async def verify_batch(scored, plan, budget):
    summary = {
        "applied": plan.verification != "off",
        "degraded": False,
        "prompt_version": RERANK_PROMPT_VERSION,
        "candidates_checked": 0,
        "match_count": 0,
        "uncertain_count": 0,
        "contradiction_count": 0,
        "unverified_count": 0,
        "visual_candidates_checked": 0,
        "visual_match_count": 0,
    }
    if plan.verification == "off":
        return [(item, "unverified") for item in scored], summary
    if not settings.search_rerank_enabled:
        summary.update(
            degraded=True,
            degraded_reason="reranker_disabled",
            unverified_count=len(scored),
        )
        return (
            []
            if plan.verification == "strict"
            else [(item, "unverified") for item in scored]
        ), summary
    # Counts verification attempts. A cancelled batch can be retried, but its
    # previous reservation remains charged; completed model stages use cache.
    await budget.reserve("candidate", len(scored))
    summary["candidates_checked"] = len(scored)
    try:
        decisions, _ = await judge_candidate_evidence(
            plan.raw_query, evidence_from_scored(scored, len(scored))
        )
    except (BudgetExhausted, RequestTimeout):
        raise
    except Exception:
        summary.update(
            degraded=True,
            degraded_reason="text_verifier_unavailable",
            unverified_count=len(scored),
        )
        return (
            []
            if plan.verification == "strict"
            else [(item, "unverified") for item in scored]
        ), summary
    if (
        plan.allow_visual
        and settings.search_visual_verify_enabled
        and visual_trigger_reason(
            plan.raw_query,
            scored,
            decisions,
            reject_confidence=settings.search_rerank_reject_confidence,
        )
    ):
        candidates = _visual_candidates(scored, decisions)
        if candidates:
            try:
                visual, _ = await judge_visual_candidates(plan.raw_query, candidates)
                summary["visual_candidates_checked"] = len(candidates)
                summary["visual_match_count"] = sum(
                    v.verdict == "match" for v in visual
                )
                decisions = merge_visual_decisions(
                    decisions,
                    visual,
                    reject_confidence=settings.search_rerank_reject_confidence,
                )
            except RequestTimeout:
                # Do not commit a partially verified batch as fully scanned.
                raise
            except BudgetExhausted:
                # Keep already obtained text evidence; stop further calls next batch.
                summary.update(degraded=True, degraded_reason="visual_budget_exhausted")
            except Exception:
                summary.update(
                    degraded=True, degraded_reason="visual_verifier_unavailable"
                )
    by_key = {d.candidate_key: d for d in decisions}
    accepted = []
    for i, item in enumerate(scored):
        decision = by_key.get(f"c{i}")
        verdict = decision.verdict if decision else "unverified"
        summary[verdict + "_count"] += 1
        if verdict == "match":
            accepted.append((item, verdict))
        elif plan.verification == "soft" and (
            verdict in {"uncertain", "unverified"}
            or (
                decision
                and decision.confidence < settings.search_rerank_reject_confidence
            )
        ):
            accepted.append((item, "uncertain" if decision else "unverified"))
    return accepted, summary
