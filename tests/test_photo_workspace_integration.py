"""Real DB ownership, atomic albums, explicit memory and conflict-safe undo."""

# ruff: noqa: F811
import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest
import httpx
from sqlalchemy import select, delete, update
from app.models.photo import Photo
from app.models.workspace import AlbumMember, PhotoWorkspace, WorkspaceAction
from app.schemas.workspace import WorkspaceCommand
from app.services.photo_workspace import (
    apply_command,
    read_workspace,
    undo_command,
    workspace_for_agent,
    version,
)
from app.services.generation_service import GenerationDomainError
from tests.test_batch1_integration import infra, add_photo  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"), reason="isolated DB required"
    ),
]


async def command(factory, user, kind, revision, **kwargs):
    async with factory() as db:
        return await apply_command(
            db,
            user,
            WorkspaceCommand(
                kind=kind,
                expected_revision=revision,
                idempotency_key=kwargs.pop("idempotency_key", uuid4().hex),
                **kwargs,
            ),
        )


@pytest.mark.asyncio
async def test_travel_twelve_save_reload_and_undo_preserves_photos(infra):
    factory, _, user = infra
    ids = [
        await add_photo(
            factory,
            user,
            status="done",
            people_count=3 if i < 5 else 0,
            ai_analysis={"scene": str(i % 6)},
            width=100,
            height=100,
        )
        for i in range(18)
    ]
    result = await command(
        factory,
        user,
        "set_task",
        0,
        task={
            "goal": "旅行精选",
            "target_count": 12,
            "min_group_count": 3,
            "locked_ids": [str(ids[-1])],
        },
    )
    result = await command(factory, user, "curate", 1, photo_ids=ids)
    assert len(result["workspace"]["selection"]) == 12
    assert result["report"]["group_count"] >= 3
    assert result["report"]["similarity"]["checked"] == 0
    assert "无法判断近似重复" in result["report"]["issues"][0]
    result = await command(factory, user, "save_album", 2, title="旅行十二张")
    album_id = result["workspace"]["albums"][0]["id"]
    async with factory() as db:
        fresh = await read_workspace(db, user)
        assert len(fresh["selection"]) == 12
        assert fresh["selection_report"]["group_count"] >= 3
        assert fresh["albums"][0]["count"] == 12 and fresh["undo"]
        result = await undo_command(db, user, UUID(result["operation_id"]), 3)
        assert not result["workspace"]["albums"]
        assert len(result["workspace"]["selection"]) == 12
        assert (
            len(
                (await db.execute(select(Photo).where(Photo.user_id == user)))
                .scalars()
                .all()
            )
            == 18
        )
        assert (
            not (
                await db.execute(
                    select(AlbumMember).where(AlbumMember.album_id == UUID(album_id))
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_stale_write_undo_conflict_and_concurrent_idempotency(infra):
    factory, _, user = infra
    pid = await add_photo(factory, user, status="done")

    async def repeat():
        return await command(
            factory,
            user,
            "add_selection",
            0,
            photo_ids=[pid],
            idempotency_key="same-action-key",
        )

    first, second = await asyncio.gather(repeat(), repeat())
    assert first["operation_id"] == second["operation_id"]
    assert second["workspace"]["revision"] == 1
    await command(
        factory,
        user,
        "set_preferences",
        1,
        preferences={"preferred_subject": "people", "title_mode": "none"},
    )
    with pytest.raises(GenerationDomainError, match="更新"):
        await command(factory, user, "clear_selection", 1)
    async with factory() as db:
        with pytest.raises(GenerationDomainError, match="后续"):
            await undo_command(db, user, UUID(first["operation_id"]), 2)
    async with factory() as db:
        context = await workspace_for_agent(db=db, user_id=user)
        assert context["explicit_preferences"]["preferred_subject"] == "people"
        assert context["selected_photos"][0]["id"] == str(pid)


@pytest.mark.asyncio
async def test_ownership_fact_versions_and_deleted_photo_undo(infra):
    factory, _, user = infra
    pid = await add_photo(factory, user, status="done")
    await command(factory, user, "add_selection", 0, photo_ids=[pid])
    async with factory() as db:
        v = version(await db.get(Photo, pid))
    result = await command(
        factory,
        user,
        "set_fact",
        1,
        photo_ids=[pid],
        photo_version=v,
        fact="这是杭州，不是苏州",
    )
    assert result["workspace"]["facts"][0]["active"]
    from app.models.user import User

    other = uuid4()
    async with factory() as db:
        db.add(User(id=other, wechat_openid=f"workspace-test-{other}"))
        await db.commit()
    try:
        with pytest.raises(GenerationDomainError):
            await command(factory, other, "add_selection", 0, photo_ids=[pid])
    finally:
        async with factory() as db:
            await db.execute(delete(User).where(User.id == other))
            await db.commit()
    async with factory() as db:
        await db.execute(
            update(Photo)
            .where(Photo.id == pid)
            .values(
                ai_description="new",
                updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
        )
        await db.commit()
        state = await read_workspace(db, user)
        assert state["selection"][0]["correction"] is None
        assert state["selection"][0]["correction_stale"]
        context = await workspace_for_agent(db=db, user_id=user)
        assert context["selected_photos"][0]["correction"] is None
    with pytest.raises(GenerationDomainError, match="版本"):
        await command(
            factory,
            user,
            "set_fact",
            2,
            photo_ids=[pid],
            photo_version=v,
            fact="旧照片修正",
        )
    result = await command(factory, user, "remove_selection", 2, photo_ids=[pid])
    async with factory() as db:
        await db.execute(delete(Photo).where(Photo.id == pid))
        await db.commit()
        with pytest.raises(GenerationDomainError, match="照片"):
            await undo_command(db, user, UUID(result["operation_id"]), 3)


@pytest.mark.asyncio
async def test_http_workspace_is_owned_and_memory_clear_can_be_undone(infra):
    from tests.test_skill_package import client_app
    from app.api.workspace import router

    factory, _, user = infra
    async with factory() as db:
        app = client_app(db, user)
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/workspace")).json()["revision"] == 0
            r = await client.post(
                "/workspace/actions",
                json={
                    "kind": "set_preferences",
                    "expected_revision": 0,
                    "idempotency_key": "memory-set-key",
                    "preferences": {"title_mode": "none"},
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["workspace"]["preferences"]["title_mode"] == "none"
            op = r.json()["operation_id"]
            assert (
                await client.post(
                    f"/workspace/actions/{op}/undo", json={"expected_revision": 1}
                )
            ).status_code == 200
            assert (await client.get("/workspace")).json()["preferences"][
                "title_mode"
            ] == "auto"
            assert (await client.get(f"/workspace/albums/{uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_undo_expiry_and_historical_memory_is_purged(infra):
    factory, _, user = infra
    result = await command(
        factory, user, "set_preferences", 0, preferences={"preferred_subject": "people"}
    )
    op = UUID(result["operation_id"])
    async with factory() as db:
        await db.execute(
            update(WorkspaceAction)
            .where(WorkspaceAction.id == op)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()
        with pytest.raises(GenerationDomainError, match="10分钟"):
            await undo_command(db, user, op, 1)
    await command(factory, user, "clear_selection", 1)
    async with factory() as db:
        assert (await db.get(WorkspaceAction, op)).before == {}
        assert (await db.get(PhotoWorkspace, user)).state["preferences"][
            "preferred_subject"
        ] == "people"


@pytest.mark.asyncio
async def test_available_thumbnails_detect_similar_candidates(
    infra, monkeypatch, tmp_path
):
    from app.services import oss
    from tests.test_package_execution import png

    factory, _, user = infra
    monkeypatch.setattr(oss, "_MOCK_ROOT", tmp_path / "oss")
    ids = []
    for index, color in enumerate(["red", "red", "blue", "green"]):
        key = f"thumb/{user}/{index}.png"
        await oss.put_object(key, png(color=color), content_type="image/png")
        ids.append(
            await add_photo(factory, user, status="done", thumb_key=key, people_count=0)
        )
    await command(factory, user, "set_task", 0, task={"target_count": 3})
    result = await command(factory, user, "curate", 1, photo_ids=ids)
    assert len(result["workspace"]["selection"]) == 3
    assert result["report"]["similarity"]["checked"] == 4
    assert result["report"]["similarity"]["groups"] == [[str(ids[0]), str(ids[1])]]
