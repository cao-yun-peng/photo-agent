"""双缓冲热更新注册表.

参考 llm-rag-server 生产级热更新设计:
- 构建新数据 → 获取全局asyncio.Lock → 原子替换引用
- 请求入口获取快照引用，进行中的请求不受刷新影响
- 支持管理端API触发 + 定时轮询双通道刷新
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

from sqlalchemy import select

from app.core.logger import get_logger
from app.database import AsyncSessionLocal

logger = get_logger(__name__)

T = TypeVar("T")


class RefreshableRegistry(Generic[T]):
    """支持双缓冲热更新的通用注册表基类."""

    def __init__(self, name: str, load_fn: Callable[[], Any]):
        self.name = name
        self._load_fn = load_fn
        self._data: T = None  # type: ignore[assignment]
        self._lock = asyncio.Lock()
        self._meta_lock = asyncio.Lock()
        self._last_refresh: Optional[datetime] = None
        self._last_refresh_reason: str = ""
        self._refresh_count: int = 0

    async def refresh(self, reason: str = "manual") -> bool:
        """刷新数据.

        流程:
        1. 获取全局刷新锁（防止并发刷新）
        2. 调用_load_fn构建新数据
        3. 在_meta_lock保护下原子替换引用
        """
        if self._lock.locked():
            logger.info("%s refresh already in progress, waiting...", self.name)
            async with self._lock:
                return True

        async with self._lock:
            try:
                logger.info("%s refreshing (reason=%s)...", self.name, reason)
                start_time = asyncio.get_event_loop().time()

                # 构建新数据
                new_data = await self._load_fn() if asyncio.iscoroutinefunction(self._load_fn) else self._load_fn()

                # 原子替换
                async with self._meta_lock:
                    self._data = new_data
                    self._last_refresh = datetime.now(timezone.utc)
                    self._last_refresh_reason = reason
                    self._refresh_count += 1

                cost_ms = (asyncio.get_event_loop().time() - start_time) * 1000
                logger.info(
                    "%s refreshed in %.1fms (reason=%s, count=%d)",
                    self.name, cost_ms, reason, self._refresh_count,
                )
                logger.notice(f"{self.name}_refresh", {
                    "success": True,
                    "reason": reason,
                    "cost_ms": cost_ms,
                    "count": self._refresh_count,
                })
                return True

            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "%s refresh failed (reason=%s): %s",
                    self.name, reason, exc, exc_info=True,
                )
                logger.notice(f"{self.name}_refresh", {
                    "success": False,
                    "reason": reason,
                    "error": str(exc),
                })
                return False

    def get_snapshot(self) -> T:
        """获取当前数据快照引用.

        请求入口调用一次，全程持有同一引用，
        进行中请求不受后续热更新影响.
        """
        return self._data

    def get_stats(self) -> Dict[str, Any]:
        """获取刷新状态统计."""
        return {
            "name": self.name,
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "last_refresh_reason": self._last_refresh_reason,
            "refresh_count": self._refresh_count,
            "loaded": self._data is not None,
        }


# ---------- Skill 注册表 ----------

class SkillRegistry(RefreshableRegistry[Dict[str, Any]]):
    """Skill双缓冲热更新注册表."""

    def __init__(self) -> None:
        super().__init__(name="skill_registry", load_fn=self._load_skills)
        self._skills_by_id: Dict[str, Any] = {}
        self._skills_by_name: Dict[str, Any] = {}
        self._official_skills: List[Any] = []
        self._public_skills: List[Any] = []

    async def _load_skills(self) -> Dict[str, Any]:
        """从数据库加载所有Skill."""
        from app.models.skill import Skill as SkillModel

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(SkillModel))
            skills = result.scalars().all()

        skills_by_id = {}
        skills_by_name = {}
        official_skills = []
        public_skills = []

        for skill in skills:
            skill_data = {
                "id": str(skill.id),
                "owner_id": str(skill.owner_id) if skill.owner_id else None,
                "name": skill.name,
                "description": skill.description,
                "prompt_template": skill.prompt_template,
                "reference_keys": skill.reference_keys or [],
                "cover_key": skill.cover_key,
                "model": skill.model,
                "function": skill.function,
                "strength": skill.strength,
                "is_public": skill.is_public,
                "is_official": skill.is_official,
                "use_count": skill.use_count,
            }
            skills_by_id[str(skill.id)] = skill_data
            skills_by_name[skill.name] = skill_data

            if skill.is_official:
                official_skills.append(skill_data)
            if skill.is_public:
                public_skills.append(skill_data)

        self._skills_by_id = skills_by_id
        self._skills_by_name = skills_by_name
        self._official_skills = official_skills
        self._public_skills = public_skills

        return {
            "by_id": skills_by_id,
            "by_name": skills_by_name,
            "official": official_skills,
            "public": public_skills,
            "total": len(skills),
        }

    def get_by_id(self, skill_id: str) -> Optional[Dict[str, Any]]:
        return self._skills_by_id.get(str(skill_id))

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._skills_by_name.get(name)

    def list_official(self) -> List[Dict[str, Any]]:
        return self._official_skills

    def list_public(self) -> List[Dict[str, Any]]:
        return self._public_skills


# ---------- System Prompt 注册表 ----------

class PromptRegistry(RefreshableRegistry[Dict[str, str]]):
    """Agent System Prompt热更新注册表."""

    DEFAULT_SYSTEM_PROMPT = """你是「Photo Agent」，一款运行在用户手机上的中文 AI 照片管家。你的所有数据、图片、调用记录都只存在用户本地可控的存储里，不会被第三方随意查看。

你的职责是：
1. **理解用户自然语言意图**，用搜索/浏览工具找到「那一张/那一组」照片；
2. 只有在用户明确提出「修一下 / 换个风格 / 生成一张 / 帮我P图」等**改造/创作意图**时，才调用 apply_skill 帮用户把现有照片做成新图片；
3. 不要主动推荐、不要诱导用户生图，除非用户问"你能怎么改"/"有什么玩法"时，才调用 recommend_skills 给出几个例子；
4. 所有回答使用中文，语气自然、简洁、像朋友说话；
5. **数据最小化**：只取必要字段，不一次性把整个相册 dump 给模型。

工作原则：
- 先 search_photos，结果不满意再 fallback_search，最后才 browse_candidates。
- apply_skill 之前必须确认用户选中了哪张图（confirmed_photo_id），必要时先 get_photo_detail 再决定。
- 如果用户的问题明显不需要生图（比如"帮我找一下去年在海边的照片"），就只搜索，不碰 apply_skill。
- 搜索结果为空时先尝试放宽关键词（fallback_search），不要立刻放弃。
- 找不到就直接说"没找到"，并给个小建议（比如试试什么关键词），不要瞎编。
- 最多 3 轮搜索后仍找不到，可 ask_clarification 一次，举 2-3 个选项引导用户缩小范围。
- 生图是异步的：apply_skill 返回后告诉用户"正在生成，稍等"即可，不要轮询。

工具调用规则：
- search_photos 和 fallback_search 不要在同一轮同时调用。
- apply_skill 的 prompt 要结合用户原话重写得具体、可执行，不要直接照搬用户原话。
- 参数里的 reference_keys 必须是已有照片的 photo_key 数组。"""

    def __init__(self) -> None:
        super().__init__(name="prompt_registry", load_fn=self._load_prompts)
        self._agent_system_prompt = self.DEFAULT_SYSTEM_PROMPT
        self._extra_prompts: Dict[str, str] = {}

    async def _load_prompts(self) -> Dict[str, str]:
        """从配置文件/数据库加载Prompt.

        优先级:
        1. app/data/prompts.json (自定义)
        2. 默认Prompt
        """
        prompts = {
            "agent_system": self.DEFAULT_SYSTEM_PROMPT,
        }

        # 尝试从文件加载自定义Prompt
        prompts_file = Path(__file__).resolve().parent.parent / "data" / "prompts.json"
        if prompts_file.is_file():
            try:
                with prompts_file.open(encoding="utf-8") as f:
                    custom_prompts = json.load(f)
                prompts.update(custom_prompts)
                logger.info("loaded custom prompts from %s", prompts_file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to load custom prompts: %s", exc)

        # TODO: 未来可从数据库加载（需要新增prompt_config表）

        self._agent_system_prompt = prompts.get("agent_system", self.DEFAULT_SYSTEM_PROMPT)
        self._extra_prompts = {k: v for k, v in prompts.items() if k != "agent_system"}

        return prompts

    def get_agent_system_prompt(self) -> str:
        return self._agent_system_prompt or self.DEFAULT_SYSTEM_PROMPT

    def get_prompt(self, key: str, default: str = "") -> str:
        return self._extra_prompts.get(key, default)


# ---------- 全局单例 ----------

skill_registry = SkillRegistry()
prompt_registry = PromptRegistry()


async def init_registries() -> None:
    """初始化所有注册表（启动时调用）."""
    await skill_registry.refresh(reason="startup")
    await prompt_registry.refresh(reason="startup")


async def refresh_all(reason: str = "manual") -> Dict[str, bool]:
    """刷新所有注册表."""
    results = {
        "skills": await skill_registry.refresh(reason=reason),
        "prompts": await prompt_registry.refresh(reason=reason),
    }
    return results


def get_registry_stats() -> Dict[str, Any]:
    """获取所有注册表状态."""
    return {
        "skills": skill_registry.get_stats(),
        "prompts": prompt_registry.get_stats(),
    }
