"""SQL equivalents of the frozen Python scoring contract."""

from datetime import datetime
from sqlalchemy import case, cast, Float, Numeric, func, literal, or_
from app.models.photo import Photo

# Never transfer vectors or unrelated processing metadata for a search result.
SEARCH_COLUMNS = (
    Photo.id,
    Photo.hash,
    Photo.updated_at,
    Photo.ai_analysis,
    Photo.ai_description,
    Photo.oss_key,
    Photo.thumb_key,
    Photo.taken_at,
    Photo.status,
)


def rounded(value, digits):
    return cast(func.round(cast(value, Numeric), digits), Float)


def score_expressions(request, plan, vector, profile):
    sem = (
        func.coalesce(
            func.greatest(0.0, 1.0 - Photo.embedding.cosine_distance(vector) / 2.0), 0.0
        )
        if vector is not None
        else literal(0.0)
    )
    # greatest(NULL,0) is 0 on Postgres; explicitly preserve missing-vector score.
    if vector is not None:
        sem = case(
            (Photo.embedding.is_(None), 0.0),
            (func.vector_norm(Photo.embedding) <= 1e-9, 0.0),
            else_=sem,
        )
    age = func.greatest(
        0.0,
        func.extract(
            "epoch", literal(datetime.fromisoformat(plan.scoring_time)) - Photo.taken_at
        )
        / 86400.0,
    )
    rec = case((Photo.taken_at.is_(None), 0.3), else_=func.exp(-age / 30.0))
    affinity = getattr(profile, "tag_affinity", None) or {}
    matched = literal(0.0)
    for tag, weight in affinity.items():
        if tag:
            matched = matched + case(
                (
                    or_(
                        Photo.ai_analysis["objects"].op("?")(tag),
                        Photo.ai_analysis["mood"].astext == tag,
                        Photo.ai_analysis["scene"].astext == tag,
                    ),
                    float(weight),
                ),
                else_=0.0,
            )
    tag_score = rounded(func.least(1.0, matched / (1.0 + matched)), 4)
    style = getattr(profile, "style_distribution", None)
    style_score = literal(0.0)
    if (
        style is not None
        and len(style) == 1024
        and sum(float(x) * float(x) for x in style) > 1e-18
    ):
        style_score = case(
            (Photo.embedding.is_(None), 0.0),
            (func.vector_norm(Photo.embedding) <= 1e-9, 0.0),
            else_=func.greatest(
                0.0, func.least(1.0, 1.0 - Photo.embedding.cosine_distance(style))
            ),
        )
    interaction = rounded(tag_score * 0.6 + style_score * 0.4, 4)
    total = max(request.w_semantic + request.w_recency + request.w_interaction, 1e-6)
    final = rounded(
        (
            sem * request.w_semantic
            + rec * request.w_recency
            + interaction * request.w_interaction
        )
        / total,
        8,
    )
    return (
        sem.label("sem"),
        rec.label("rec"),
        interaction.label("inter"),
        final.label("final"),
    )
