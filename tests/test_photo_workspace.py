"""Deterministic selection without fabricated candidates or inferred memory."""

from types import SimpleNamespace
from uuid import uuid4
from app.schemas.workspace import TaskMemory, ExplicitPreferences
from app.services.photo_workspace import curate


def candidate(group=False, scene="landscape", image_hash=None):
    return SimpleNamespace(
        id=uuid4(),
        hash=image_hash or uuid4().hex,
        people_count=3 if group else 0,
        ai_analysis={"scene": scene},
        width=100,
        height=100,
    )


def test_twelve_photos_preserve_locked_and_group_minimum():
    pool = [candidate(i < 5, str(i % 6)) for i in range(20)]
    task = TaskMemory(target_count=12, min_group_count=3, locked_ids=[pool[-1].id])
    ids, report = curate(pool, task, ExplicitPreferences())
    assert len(ids) == len(set(ids)) == 12 and ids[0] == str(pool[-1].id)
    assert report["group_count"] >= 3 and not report["issues"]


def test_dedup_shortage_and_exclusion_are_honest():
    first = candidate(image_hash="same")
    pool = [first, candidate(image_hash="same"), candidate()]
    ids, report = curate(
        pool,
        TaskMemory(target_count=12, min_group_count=3, excluded_ids=[pool[-1].id]),
        ExplicitPreferences(),
    )
    assert ids == [str(first.id)]
    assert len(report["issues"]) == 2


def test_similarity_groups_keep_one_but_explicit_locks_override():
    pool = [candidate() for _ in range(4)]
    groups = {str(pool[0].id): 0, str(pool[1].id): 0}
    ids, _ = curate(pool, TaskMemory(target_count=4), ExplicitPreferences(), groups)
    assert len(ids) == 3
    ids, _ = curate(
        pool,
        TaskMemory(target_count=4, locked_ids=[pool[0].id, pool[1].id]),
        ExplicitPreferences(),
        groups,
    )
    assert len(ids) == 4


def test_thumbnail_similarity_checks_color_and_aspect_not_just_flat_hash():
    from app.services.selection_similarity import fingerprint, similar
    from tests.test_package_execution import png

    red = fingerprint(png(color="red"))
    assert similar(red, red)
    assert not similar(red, fingerprint(png(color="blue")))
