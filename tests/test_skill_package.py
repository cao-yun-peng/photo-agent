"""Offline importer and API boundaries; never executes package contents."""

import io
import stat
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.api import skill_packages as api
from app.core.security import get_current_user
from app.database import get_db
from app.models.skill import Skill, SkillAsset, SkillVersion
from app.services.skill_package import PackageError, inspect_package


def archive(files=None, compression=zipfile.ZIP_STORED):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression) as z:
        for path, content in (
            files
            or {
                "SKILL.md": "---\nname: test\ndescription: Test package\n---\nRead `references/style.md`.",
                "references/style.md": "Style instructions",
            }
        ).items():
            z.writestr(path, content)
    return result.getvalue()


def test_nested_package_hash_independent_of_zip_wrapper():
    original = inspect_package(archive())
    nested = inspect_package(
        archive({"outer/skill/" + k: v for k, v in original.files.items()})
    )
    assert nested.report.root == "outer/skill"
    assert nested.report.content_sha256 == original.report.content_sha256
    assert nested.report.references["SKILL.md"] == ["references/style.md"]
    assert nested.report.execution_status == "planning_available"


@pytest.mark.parametrize(
    "path",
    ["../escape", "/escape", "C:/escape", "a\\escape", "a/../escape", "a./escape"],
)
def test_unsafe_paths_rejected(path):
    with pytest.raises(PackageError):
        inspect_package(archive({"SKILL.md": "x", path: "bad"}))


def test_duplicates_links_multiple_roots_and_bombs_rejected():
    with pytest.raises(PackageError, match="重复"):
        inspect_package(archive({"SKILL.md": "a", "skill.md": "b"}))
    with pytest.raises(PackageError, match="一个"):
        inspect_package(archive({"a/SKILL.md": "a", "b/SKILL.md": "b"}))
    link = zipfile.ZipInfo("SKILL.md")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(PackageError, match="链接"):
        inspect_package(archive({link: "target"}))
    with pytest.raises(PackageError, match="压缩比"):
        inspect_package(archive({"SKILL.md": "x" * 100_000}, zipfile.ZIP_DEFLATED))


def test_missing_resource_script_and_external_reference_report():
    parsed = inspect_package(
        archive(
            {
                "SKILL.md": "---\nname: test\ndescription: test\n---\nRead `references/missing.md` and [web](https://example.com/spec.md)",
                "scripts/run.py": "raise RuntimeError('never execute')",
            }
        )
    )
    assert not parsed.report.can_import
    assert any("缺失" in e for e in parsed.report.errors)
    assert any("脚本" in e for e in parsed.report.errors)
    assert any("不访问" in e for e in parsed.report.warnings)


@pytest.mark.parametrize(
    "header",
    [
        "name: &a test\ndescription: *a",
        "name: !!python/object:builtins.str {}\ndescription: x",
        "name: x\ndescription: [x]",
    ],
)
def test_yaml_aliases_tags_invalid_types_rejected(header):
    with pytest.raises(PackageError):
        inspect_package(archive({"SKILL.md": f"---\n{header}\n---\nHi"}))


def test_image_verified_and_reference_preserved():
    image = io.BytesIO()
    Image.new("RGB", (8, 6)).save(image, format="PNG")
    files = dict(inspect_package(archive()).files)
    files["assets/style.png"] = image.getvalue()
    parsed = inspect_package(archive(files))
    assert parsed.report.cover_data_url.startswith("data:image/png;base64,")
    files["assets/style.png"] = b"not an image"
    with pytest.raises(PackageError, match="图片"):
        inspect_package(archive(files))


def test_entry_and_expanded_size_limits(monkeypatch):
    from app.services import skill_package as parser

    monkeypatch.setattr(parser, "MAX_FILES", 1)
    with pytest.raises(PackageError, match="条目"):
        inspect_package(archive())
    monkeypatch.setattr(parser, "MAX_FILES", 128)
    monkeypatch.setattr(parser, "MAX_TOTAL", 1)
    with pytest.raises(PackageError, match="上限"):
        inspect_package(archive())


@pytest.mark.asyncio
async def test_request_limit_and_unauthenticated_preview(monkeypatch):
    monkeypatch.setattr(api, "MAX_ARCHIVE", 2)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client_app(None, uuid4())),
        base_url="http://test",
    ) as client:
        assert (
            await client.post("/skills/packages/preview", content=b"large")
        ).status_code == 413
    app = FastAPI()
    app.include_router(api.router, prefix="/skills")
    from app.core.errors import ApiError
    from app.main import api_error_handler

    app.add_exception_handler(ApiError, api_error_handler)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post("/skills/packages/preview", content=b"large")
        # Existing auth contract uses an error envelope, sometimes with HTTP 200.
        assert denied.json()["errNo"] == 10002
        assert "content_sha256" not in denied.json()


def test_manifest_icon_references_and_disguised_scripts_are_not_silently_accepted():
    files = dict(inspect_package(archive()).files)
    files["agents/openai.yaml"] = b"interface:\n  icon_small: ./assets/missing.png\n"
    files["LICENSE.py"] = b"print('not a license')"
    parsed = inspect_package(archive(files))
    assert not parsed.report.can_import
    assert any("missing.png" in e for e in parsed.report.errors)
    assert any("LICENSE.py" in e for e in parsed.report.errors)


def result(value):
    return SimpleNamespace(
        scalar_one_or_none=lambda: value,
        scalar_one=lambda: value,
        scalars=lambda: SimpleNamespace(all=lambda: value),
    )


def client_app(db, owner):
    app = FastAPI()
    app.include_router(api.router, prefix="/skills")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner)
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.mark.asyncio
async def test_preview_then_import_persists_private_version_and_assets():
    owner = uuid4()
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[result(owner), result(None), result(0)]),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client_app(db, owner)), base_url="http://test"
    ) as client:
        preview = await client.post("/skills/packages/preview", content=archive())
        assert preview.status_code == 200
        db.execute.assert_not_awaited()
        digest = preview.json()["content_sha256"]
        saved = await client.post(
            "/skills/packages/import",
            params={"expected_hash": digest},
            content=archive(),
        )
    assert saved.status_code == 200
    objects = [c.args[0] for c in db.add.call_args_list]
    skill = next(v for v in objects if isinstance(v, Skill))
    version = next(v for v in objects if isinstance(v, SkillVersion))
    assert skill.owner_id == owner and skill.kind == "package" and not skill.is_public
    assert skill.current_version_id == version.id
    assert "cover_data_url" not in version.report
    assert len([v for v in objects if isinstance(v, SkillAsset)]) == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_hash_mismatch_stops_before_writes_and_old_duplicate_stays_inert():
    owner = uuid4()
    old = SimpleNamespace(id=uuid4(), skill_id=uuid4())
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[result(owner), result(old)]),
        add=Mock(),
        commit=AsyncMock(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client_app(db, owner)), base_url="http://test"
    ) as client:
        mismatch = await client.post(
            "/skills/packages/import",
            params={"expected_hash": "0" * 64},
            content=archive(),
        )
        assert mismatch.status_code == 409
        db.execute.assert_not_awaited()
        duplicate = await client.post(
            "/skills/packages/import",
            params={"expected_hash": inspect_package(archive()).report.content_sha256},
            content=archive(),
        )
    assert duplicate.json()["deduplicated"]
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_owner_version_and_asset_access_fail_closed():
    owner, skill_id, version_id = uuid4(), uuid4(), uuid4()
    db = SimpleNamespace(execute=AsyncMock(return_value=result(None)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client_app(db, owner)), base_url="http://test"
    ) as client:
        assert (await client.get(f"/skills/{skill_id}/versions")).status_code == 404
        assert (
            await client.get(
                f"/skills/{skill_id}/versions/{version_id}/assets",
                params={"path": "SKILL.md"},
            )
        ).status_code == 404
    for call in db.execute.call_args_list:
        assert owner in call.args[0].compile().params.values()


@pytest.mark.asyncio
async def test_invalid_package_cannot_prepare_paid_generation():
    from app.services.generation_service import (
        prepare_generation,
        GenerationDomainError,
    )

    owner = uuid4()
    skill = SimpleNamespace(
        id=uuid4(),
        owner_id=owner,
        is_public=False,
        is_official=False,
        model="gpt-image-2",
        kind="package",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[result(SimpleNamespace(id=uuid4())), result(skill)]
        ),
        add=Mock(),
    )
    with pytest.raises(GenerationDomainError, match="创作方案"):
        await prepare_generation(
            db=db, user_id=owner, photo_id=uuid4(), skill_id=skill.id
        )
    db.add.assert_not_called()
