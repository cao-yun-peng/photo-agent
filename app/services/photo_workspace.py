"""Explicit, versioned workspace commands. No inferred long-term memory writes."""

import copy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from sqlalchemy import select, delete, func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import load_only
from app.models.workspace import PhotoWorkspace, Album, AlbumMember, WorkspaceAction
from app.models.photo import Photo
from app.models.user import User
from app.schemas.workspace import TaskMemory, ExplicitPreferences
from app.services.package_execution import digest
from app.services.oss import sign_get_url
from app.services.generation_service import GenerationDomainError

Error = GenerationDomainError


def initial_state():
    return {
        "selection": [],
        "task": TaskMemory().model_dump(mode="json"),
        "preferences": ExplicitPreferences().model_dump(),
        "facts": {},
        "selection_report": None,
        "task_updated_at": None,
        "preferences_updated_at": None,
    }


def version(photo):
    return digest([photo.hash, photo.oss_key, photo.updated_at.isoformat()])


async def photos(db, user_id, ids, strict=False):
    ids = list(dict.fromkeys(str(i) for i in ids))
    if not ids:
        return []
    query = (
        select(Photo)
        .options(
            load_only(
                Photo.id,
                Photo.user_id,
                Photo.hash,
                Photo.oss_key,
                Photo.thumb_key,
                Photo.updated_at,
                Photo.ai_description,
                Photo.ai_analysis,
                Photo.taken_at,
                Photo.people_count,
                Photo.width,
                Photo.height,
            )
        )
        .where(
            Photo.user_id == user_id,
            Photo.id.in_([UUID(i) for i in ids]),
            Photo.status.in_(["done", "partial_done"]),
        )
    )
    if strict:
        query = query.with_for_update(read=True)
    rows = (await db.execute(query)).scalars().all()
    mapping = {str(p.id): p for p in rows}
    if strict and set(mapping) != set(ids):
        raise Error("photo_unavailable", "照片已删除、不可用或不属于当前用户", 409)
    return [mapping[i] for i in ids if i in mapping]


def photo_out(p, state):
    fact = state.get("facts", {}).get(str(p.id))
    return {
        "id": str(p.id),
        "version": version(p),
        "thumb_url": sign_get_url(p.thumb_key or p.oss_key),
        "description": p.ai_description,
        "correction": fact if fact and fact["photo_version"] == version(p) else None,
        "correction_stale": bool(fact and fact["photo_version"] != version(p)),
    }


async def read_workspace(db, user_id):
    workspace = (
        await db.execute(
            select(PhotoWorkspace)
            .where(PhotoWorkspace.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    state = copy.deepcopy(workspace.state) if workspace else initial_state()
    selected = await photos(db, user_id, state["selection"])
    albums = (
        (
            await db.execute(
                select(Album)
                .where(Album.user_id == user_id, Album.deleted.is_(False))
                .order_by(Album.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    counts = (
        dict(
            (
                await db.execute(
                    select(AlbumMember.album_id, func.count())
                    .join(Photo, Photo.id == AlbumMember.photo_id)
                    .where(
                        AlbumMember.album_id.in_([a.id for a in albums]),
                        Photo.user_id == user_id,
                        Photo.status.in_(["done", "partial_done"]),
                    )
                    .group_by(AlbumMember.album_id)
                )
            ).all()
        )
        if albums
        else {}
    )
    undo = None
    if workspace:
        latest = (
            await db.execute(
                select(WorkspaceAction)
                .where(
                    WorkspaceAction.user_id == user_id,
                    WorkspaceAction.revision == workspace.revision,
                    WorkspaceAction.undone.is_(False),
                    WorkspaceAction.expires_at > datetime.now(timezone.utc),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest:
            undo = {
                "operation_id": str(latest.id),
                "expires_at": latest.expires_at.isoformat(),
            }
    fact_photos = await photos(db, user_id, state["facts"].keys())
    facts = [
        {
            "photo_id": str(p.id),
            "current_version": version(p),
            "active": state["facts"][str(p.id)]["photo_version"] == version(p),
            **state["facts"][str(p.id)],
        }
        for p in fact_photos
    ]
    return {
        "revision": workspace.revision if workspace else 0,
        "selection": [photo_out(p, state) for p in selected],
        "selection_report": (
            {"issues": ["选片中有照片已不可用，请重新核对目标。"]}
            if len(selected) != len(state["selection"])
            else state.get("selection_report")
        ),
        "undo": undo,
        "facts": facts,
        "task": state["task"],
        "preferences": state["preferences"],
        "memory_source": "explicit_user",
        "task_updated_at": state.get("task_updated_at"),
        "preferences_updated_at": state.get("preferences_updated_at"),
        "updated_at": workspace.updated_at.isoformat() if workspace else None,
        "albums": [
            {"id": str(a.id), "title": a.title, "count": counts.get(a.id, 0)}
            for a in albums
        ],
        "missing_selection_count": len(state["selection"]) - len(selected),
    }


async def get_album(db, user_id, album_id):
    a = (
        await db.execute(
            select(Album).where(
                Album.id == album_id, Album.user_id == user_id, Album.deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    if not a:
        raise Error("album_not_found", "相册不存在", 404)
    ids = (
        (
            await db.execute(
                select(AlbumMember.photo_id)
                .where(AlbumMember.album_id == a.id)
                .order_by(AlbumMember.position)
            )
        )
        .scalars()
        .all()
    )
    return a, [str(i) for i in ids]


async def locked_workspace(db, user_id):
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    await db.execute(
        update(WorkspaceAction)
        .where(
            WorkspaceAction.user_id == user_id,
            WorkspaceAction.expires_at <= datetime.now(timezone.utc),
        )
        .values(before={})
    )
    await db.execute(
        insert(PhotoWorkspace)
        .values(user_id=user_id, revision=0, state=initial_state())
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    return (
        await db.execute(
            select(PhotoWorkspace)
            .where(PhotoWorkspace.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def write_members(db, album_id, ids):
    await db.execute(delete(AlbumMember).where(AlbumMember.album_id == album_id))
    for i, pid in enumerate(ids):
        db.add(AlbumMember(album_id=album_id, photo_id=UUID(str(pid)), position=i))
    await db.flush()


def curate(candidates, task, preferences, similar_groups=None):
    similar_groups = similar_groups or {}
    mapping = {str(p.id): p for p in candidates}
    locked = [str(i) for i in task.locked_ids]
    if not set(locked) <= set(mapping):
        raise Error("locked_photo_missing", "固定照片已不可用，请先更新任务约束", 409)
    selected = [mapping[i] for i in locked]
    excluded = {str(i) for i in task.excluded_ids}
    pool = [
        p for p in candidates if str(p.id) not in excluded and str(p.id) not in locked
    ]
    seen = {p.hash for p in selected}
    seen_groups = {
        similar_groups[str(p.id)] for p in selected if str(p.id) in similar_groups
    }
    scenes = {str((p.ai_analysis or {}).get("scene") or "unknown") for p in selected}
    while pool and len(selected) < task.target_count:
        need_group = (
            sum((p.people_count or 0) >= 2 for p in selected) < task.min_group_count
        )

        def rank(p):
            scene = str((p.ai_analysis or {}).get("scene") or "unknown")
            group = (p.people_count or 0) >= 2
            preferred = (
                (p.people_count == 0)
                if task.prefer_landscape or preferences.preferred_subject == "landscape"
                else (p.people_count or 0) > 0
                if preferences.preferred_subject == "people"
                else False
            )
            return (
                int(need_group and group),
                int(scene not in scenes),
                int(preferred),
                (p.width or 0) * (p.height or 0),
            )

        pool.sort(
            key=rank, reverse=True
        )  # stable ties preserve the user's candidate order
        photo = pool.pop(0)
        if photo.hash in seen or (
            str(photo.id) in similar_groups
            and similar_groups[str(photo.id)] in seen_groups
        ):
            continue
        selected.append(photo)
        if str(photo.id) in similar_groups:
            seen_groups.add(similar_groups[str(photo.id)])
        seen.add(photo.hash)
        scenes.add(str((photo.ai_analysis or {}).get("scene") or "unknown"))
    groups = sum((p.people_count or 0) >= 2 for p in selected)
    issues = []
    if len(selected) < task.target_count:
        issues.append(
            f"可用候选不足：目标{task.target_count}张，选出{len(selected)}张。"
        )
    if groups < task.min_group_count:
        issues.append(f"合照不足：至少需要{task.min_group_count}张，选出{groups}张。")
    return [str(p.id) for p in selected], {
        "selected_count": len(selected),
        "group_count": groups,
        "issues": issues,
        "method": "fixed photos, known group counts, scene coverage, explicit preference, resolution; exact hash and available thumbnail similarity hints",
    }


async def apply_command(db, user_id, command):
    w = await locked_workspace(db, user_id)
    request_hash = digest(command.model_dump(mode="json"))
    previous = (
        await db.execute(
            select(WorkspaceAction).where(
                WorkspaceAction.user_id == user_id,
                WorkspaceAction.idempotency_key == command.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if previous:
        if previous.request_hash != request_hash:
            raise Error("idempotency_conflict", "请求标识对应不同操作", 409)
        return {
            "workspace": await read_workspace(db, user_id),
            "operation_id": str(previous.id),
            "can_undo": not previous.undone
            and previous.revision == w.revision
            and previous.expires_at > datetime.now(timezone.utc),
            "report": None,
        }
    if w.revision != command.expected_revision:
        raise Error(
            "workspace_conflict", "选片或记忆已被其他操作更新，请刷新后重试", 409
        )
    before = {"state": copy.deepcopy(w.state), "album": None}
    state = copy.deepcopy(w.state)
    state["selection"] = [
        str(p.id) for p in await photos(db, user_id, state["selection"])
    ]
    ids = list(dict.fromkeys(str(i) for i in command.photo_ids))
    kind = command.kind
    report = None
    if kind in {"add_selection", "remove_selection"}:
        if not ids:
            raise Error("photos_required", "请选择照片", 422)
        if kind == "add_selection":
            await photos(db, user_id, ids, True)
            state["selection"] = list(dict.fromkeys(state["selection"] + ids))
            state["task"]["excluded_ids"] = [
                i for i in state["task"]["excluded_ids"] if i not in ids
            ]
        else:
            state["selection"] = [i for i in state["selection"] if i not in ids]
            state["task"]["locked_ids"] = [
                i for i in state["task"]["locked_ids"] if i not in ids
            ]
            state["task"]["excluded_ids"] = list(
                dict.fromkeys(state["task"]["excluded_ids"] + ids)
            )[-100:]
    elif kind == "clear_selection":
        state["selection"] = []
        state["task"] = TaskMemory().model_dump(mode="json")
    elif kind == "set_task":
        if command.task is None:
            raise Error("task_required", "缺少任务目标", 422)
        await photos(db, user_id, command.task.locked_ids, True)
        state["task"] = command.task.model_dump(mode="json")
    elif kind == "set_preferences":
        if command.preferences is None:
            raise Error("preferences_required", "缺少明确偏好", 422)
        state["preferences"] = command.preferences.model_dump()
    elif kind == "clear_facts":
        state["facts"] = {}
    elif kind in {"set_fact", "clear_fact"}:
        if len(ids) != 1:
            raise Error("one_photo_required", "请选择一张照片", 422)
        photo = (await photos(db, user_id, ids, True))[0]
        if command.photo_version != version(photo):
            raise Error("photo_version_conflict", "照片版本已变化，请重新核对", 409)
        if kind == "clear_fact":
            state["facts"].pop(ids[0], None)
        else:
            if not command.fact or not command.fact.strip():
                raise Error("fact_required", "请填写事实修正", 422)
            if ids[0] not in state["facts"] and len(state["facts"]) >= 100:
                raise Error("memory_limit", "最多保存100张照片的明确修正", 409)
            state["facts"][ids[0]] = {
                "value": command.fact.strip(),
                "photo_version": version(photo),
                "source": "explicit_user",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
    elif kind == "curate":
        task = TaskMemory.model_validate(state["task"])
        candidates = await photos(
            db,
            user_id,
            list(dict.fromkeys([str(i) for i in task.locked_ids] + ids)),
            True,
        )
        from app.services.selection_similarity import thumbnail_groups

        groups, similarity_report = await thumbnail_groups(candidates)
        state["selection"], report = curate(
            candidates,
            task,
            ExplicitPreferences.model_validate(state["preferences"]),
            groups,
        )
        report["similarity"] = similarity_report
        if similarity_report["groups"]:
            report["issues"].append(
                f"发现{len(similarity_report['groups'])}组近似缩略图，优先保留各组一张；固定照片不受此限制，请人工核对。"
            )
        if similarity_report["checked"] < similarity_report["total"]:
            report["issues"].append(
                f"仅检查{similarity_report['checked']}/{similarity_report['total']}张缩略图，其他图片无法判断近似重复。"
            )
    elif kind in {"save_album", "delete_album", "load_album"}:
        if command.album_id:
            album, members = await get_album(db, user_id, command.album_id)
            before["album"] = {
                "id": str(album.id),
                "title": album.title,
                "deleted": False,
                "members": members,
            }
        elif kind == "save_album":
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Album)
                    .where(Album.user_id == user_id)
                )
            ).scalar_one()
            if count >= 200:
                raise Error("album_limit", "相册数量达到上限", 409)
            album = Album(id=uuid4(), user_id=user_id, title="untitled")
            db.add(album)
            await db.flush()
            before["album"] = {
                "id": str(album.id),
                "title": "untitled",
                "deleted": True,
                "members": [],
            }
        else:
            raise Error("album_required", "请选择相册", 422)
        if kind == "load_album":
            state["selection"] = [str(p.id) for p in await photos(db, user_id, members)]
            state["task"] = TaskMemory().model_dump(mode="json")
            before["album"] = None
        elif kind == "delete_album":
            album.deleted = True
        else:
            if not command.title or not command.title.strip():
                raise Error("title_required", "请填写相册名称", 422)
            if not state["selection"]:
                raise Error("selection_empty", "请先选片", 409)
            await photos(db, user_id, state["selection"], True)
            album.title = command.title.strip()
            await write_members(db, album.id, state["selection"])
    if kind == "curate":
        state["selection_report"] = report
    elif kind in {
        "add_selection",
        "remove_selection",
        "clear_selection",
        "load_album",
        "set_task",
    }:
        state["selection_report"] = None
    if len(state["selection"]) > 100:
        raise Error("selection_limit", "选片区最多100张", 409)
    if kind in {"set_task", "remove_selection", "clear_selection", "load_album"}:
        state["task_updated_at"] = datetime.now(timezone.utc).isoformat()
    if kind == "set_preferences":
        state["preferences_updated_at"] = datetime.now(timezone.utc).isoformat()
    w.state = state
    w.revision += 1
    action = WorkspaceAction(
        user_id=user_id,
        idempotency_key=command.idempotency_key,
        request_hash=request_hash,
        revision=w.revision,
        before=before,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(action)
    await db.flush()
    response_workspace = await read_workspace(db, user_id)
    await db.commit()
    return {
        "workspace": response_workspace,
        "operation_id": str(action.id),
        "can_undo": True,
        "report": report,
    }


async def undo_command(db, user_id, operation_id, expected_revision):
    w = await locked_workspace(db, user_id)
    action = (
        await db.execute(
            select(WorkspaceAction)
            .where(
                WorkspaceAction.id == operation_id, WorkspaceAction.user_id == user_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not action:
        raise Error("operation_not_found", "操作不存在", 404)
    if action.undone:
        return {
            "workspace": await read_workspace(db, user_id),
            "operation_id": str(action.id),
            "can_undo": False,
            "report": None,
        }
    if w.revision != expected_revision or w.revision != action.revision:
        raise Error("undo_conflict", "后续已有修改，不能撤销覆盖；请手动调整", 409)
    if action.expires_at <= datetime.now(timezone.utc):
        raise Error("undo_expired", "撤销已超过10分钟有效期", 409)
    before = action.before
    # Never resurrect deleted/unowned members, including fixed task photos.
    await photos(
        db,
        user_id,
        before["state"]["selection"] + before["state"]["task"]["locked_ids"],
        True,
    )
    album = before["album"]
    if album:
        await photos(db, user_id, album["members"], True)
        row = (
            await db.execute(
                select(Album)
                .where(Album.id == UUID(album["id"]), Album.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one()
        row.title = album["title"]
        row.deleted = album["deleted"]
        await write_members(db, row.id, album["members"])
    w.state = copy.deepcopy(before["state"])
    w.revision += 1
    action.undone = True
    action.before = {}
    await db.flush()
    response_workspace = await read_workspace(db, user_id)
    await db.commit()
    return {
        "workspace": response_workspace,
        "operation_id": str(action.id),
        "can_undo": False,
        "report": None,
    }


async def workspace_for_agent(*, db, user_id, **kwargs):
    result = await read_workspace(db, user_id)
    # Source-labelled facts are data, never tool authority or generation approval.
    return {
        "ok": True,
        "workspace_revision": result["revision"],
        "task_memory": result["task"],
        "explicit_preferences": result["preferences"],
        "selected_photos": [
            {k: v for k, v in p.items() if k != "thumb_url"}
            for p in result["selection"]
        ],
        "albums": result["albums"],
        "hint": "明确用户记忆；本轮要求优先。选片不等于授权生成，写入与撤销请在选片工作区操作。",
    }


async def purge_workspace_history(ctx):
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        ids = (
            (
                await db.execute(
                    select(WorkspaceAction.id)
                    .where(
                        WorkspaceAction.expires_at <= datetime.now(timezone.utc),
                        WorkspaceAction.before != {},
                    )
                    .order_by(WorkspaceAction.expires_at)
                    .with_for_update(skip_locked=True)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
        if ids:
            await db.execute(
                update(WorkspaceAction)
                .where(WorkspaceAction.id.in_(ids))
                .values(before={})
            )
        await db.commit()
        return {"purged": len(ids)}
