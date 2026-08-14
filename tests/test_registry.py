"""热更新注册表回归测试。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_skill_registry_loads_current_skill_schema(monkeypatch) -> None:
    import app.core.registry as registry_module

    skill = SimpleNamespace(
        id=uuid4(),
        owner_id=None,
        name="测试技能",
        description="描述",
        prompt_template="prompt",
        reference_keys=[],
        cover_key=None,
        model="wanx2.1-imageedit",
        function="description_edit",
        strength=0.7,
        is_public=True,
        is_official=True,
        use_count=0,
    )

    class ScalarResult:
        def all(self):
            return [skill]

    class Result:
        def scalars(self):
            return ScalarResult()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, statement):
            assert "is_deleted" not in str(statement)
            return Result()

    monkeypatch.setattr(registry_module, "AsyncSessionLocal", FakeSession)
    registry = registry_module.SkillRegistry()
    loaded = await registry._load_skills()
    assert loaded["total"] == 1
    assert registry.get_by_name("测试技能")["is_official"] is True
