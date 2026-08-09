"""手动播种/更新官方 Skill（从 app/data/official_skills.json 读取）。

用法：
  docker compose exec api python scripts/seed_skills.py

与 main.py 启动时的 _seed_official_skills() 逻辑一致：
  - 新增的 Skill → 插入（is_official=True, is_public=True）
  - 已存在的官方 Skill → 更新 description/prompt_template/model（保留 use_count）
  - 已存在的用户自建同名 Skill → 跳过

迭代方式：编辑 app/data/official_skills.json → 运行此脚本 或 重启服务。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models.skill import Skill  # noqa: E402


async def main() -> None:
    skills_file = ROOT / "app" / "data" / "official_skills.json"
    if not skills_file.is_file():
        print(f"❌ 文件不存在: {skills_file}")
        sys.exit(1)

    with skills_file.open(encoding="utf-8") as f:
        official_skills = json.load(f)

    inserted = 0
    updated = 0
    skipped = 0

    async with AsyncSessionLocal() as db:
        for spec in official_skills:
            existing = (
                await db.execute(select(Skill).where(Skill.name == spec["name"]))
            ).scalar_one_or_none()

            if existing is None:
                db.add(Skill(
                    owner_id=None,
                    name=spec["name"],
                    description=spec.get("description", ""),
                    prompt_template=spec["prompt_template"],
                    reference_keys=spec.get("reference_keys", []),
                    cover_key=spec.get("cover_key"),
                    model=spec.get("model", "wanx2.1-imageedit"),
                    function=spec.get("function", "description_edit"),
                    strength=spec.get("strength", 0.7),
                    is_public=True,
                    is_official=True,
                ))
                inserted += 1
            elif existing.is_official:
                existing.description = spec.get("description", existing.description)
                existing.prompt_template = spec["prompt_template"]
                existing.model = spec.get("model", existing.model)
                existing.function = spec.get("function", existing.function)
                existing.strength = spec.get("strength", existing.strength)
                if "reference_keys" in spec:
                    existing.reference_keys = spec["reference_keys"]
                updated += 1
            else:
                skipped += 1

        if inserted or updated:
            await db.commit()

    print(f"✓ inserted={inserted}  updated={updated}  skipped(user)={skipped}  total={len(official_skills)}")


if __name__ == "__main__":
    asyncio.run(main())
