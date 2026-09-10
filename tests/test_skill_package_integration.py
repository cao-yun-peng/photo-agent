"""Real PostgreSQL persistence checks; requires the isolated migrated test DB."""

import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.core.security import get_current_user
from tests.test_batch1_integration import infra  # noqa: F401
from tests.test_skill_package import archive, client_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("PHOTO_AGENT_TEST_DATABASE_URL"),
        reason="isolated test DB not configured",
    ),
]


@pytest.mark.asyncio
async def test_versions_are_durable_immutable_and_private(infra):  # noqa: F811
    factory, _, owner = infra
    async with factory() as db:
        app = client_app(db, owner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = archive()
            preview = (
                await client.post("/skills/packages/preview", content=first)
            ).json()
            saved = await client.post(
                "/skills/packages/import",
                params={"expected_hash": preview["content_sha256"]},
                content=first,
            )
            assert saved.status_code == 200
            sid, vid = saved.json()["skill_id"], saved.json()["version_id"]
            second = archive(
                {
                    "SKILL.md": "---\nname: updated\ndescription: updated\n---\nNew version"
                }
            )
            preview2 = (
                await client.post("/skills/packages/preview", content=second)
            ).json()
            updated = await client.post(
                "/skills/packages/import",
                params={"expected_hash": preview2["content_sha256"], "skill_id": sid},
                content=second,
            )
            assert updated.status_code == 200 and updated.json()["version_id"] != vid
            versions = (await client.get(f"/skills/{sid}/versions")).json()
            assert len(versions) == 2
            old = await client.get(
                f"/skills/{sid}/versions/{vid}/assets",
                params={"path": "references/style.md"},
            )
            assert old.status_code == 200 and old.text == "Style instructions"
            replay = await client.post(
                "/skills/packages/import",
                params={"expected_hash": preview["content_sha256"], "skill_id": sid},
                content=first,
            )
            assert replay.json()["deduplicated"]
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=uuid4()
            )
            assert (await client.get(f"/skills/{sid}/versions")).status_code == 404
            assert (
                await client.get(
                    f"/skills/{sid}/versions/{vid}/assets", params={"path": "SKILL.md"}
                )
            ).status_code == 404


@pytest.mark.asyncio
async def test_package_recommendation_is_owned_and_requires_valid_version(infra):  # noqa: F811
    from uuid import UUID
    from app.services.recommend import recommend_skills
    from app.models.skill import Skill

    factory, _, owner = infra
    async with factory() as db:
        app = client_app(db, owner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            data = archive()
            preview = (
                await client.post("/skills/packages/preview", content=data)
            ).json()
            saved = await client.post(
                "/skills/packages/import",
                content=data,
                params={"expected_hash": preview["content_sha256"]},
            )
            assert saved.status_code == 200
            sid = saved.json()["skill_id"]
        rows = await recommend_skills(db, owner, limit=1000)
        package = next(row for row in rows if row["id"] == sid)
        assert package["requires_plan_review"] and package["kind"] == "package"
        skill = await db.get(Skill, UUID(sid))
        # Even legacy/corrupt public flags cannot expose a private package.
        skill.is_public = True
        skill.is_official = True
        await db.commit()
        assert sid not in {
            row["id"] for row in await recommend_skills(db, uuid4(), limit=1000)
        }
        skill.current_version_id = None
        await db.commit()
        assert sid not in {
            row["id"] for row in await recommend_skills(db, owner, limit=1000)
        }
