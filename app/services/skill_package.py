"""Bounded, offline ZIP inspection. No extraction, commands or remote reads."""

import base64
import hashlib
import io
import json
import posixpath
import re
import stat
import unicodedata
import warnings
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml
from PIL import Image

from app.schemas.skill_package import PackageAssetInfo, PackageReport

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_TOTAL = 24 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 128
MAX_TEXT = 128 * 1024
IMAGE_TYPES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
LICENSE_NAMES = {"LICENSE", "LICENSE.TXT", "LICENSE.MD", "COPYING", "COPYING.TXT"}


class PackageError(ValueError):
    pass


@dataclass
class ParsedPackage:
    report: PackageReport
    files: dict[str, bytes]
    instructions: str


def _path(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    if (
        not name
        or len(name) > 240
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 for c in name)
        or name.startswith("/")
        or any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in name.split("/"))
    ):
        raise PackageError("ZIP包含不安全或不明确的路径")
    return name


def _yaml(text: str):
    # Aliases/anchors can create recursive or exponentially expanded metadata.
    try:
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                raise PackageError("元数据不支持YAML锚点或别名")
        value = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise PackageError("YAML元数据格式无效") from exc
    if not isinstance(value, dict):
        raise PackageError("元数据必须是对象")
    return value


def inspect_package(data: bytes) -> ParsedPackage:
    if len(data) > MAX_ARCHIVE:
        raise PackageError("ZIP不得超过16 MiB")
    files = {}
    seen = set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise PackageError("ZIP最多128个条目")
            total = 0
            for item in infos:
                if item.orig_filename != item.filename:
                    raise PackageError("ZIP文件名含非法控制字符")
                name = _path(
                    item.filename.rstrip("/") if item.is_dir() else item.filename
                )
                key = name.casefold()
                if key in seen:
                    raise PackageError("ZIP包含重复路径")
                seen.add(key)
                mode = stat.S_IFMT(item.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                    raise PackageError("不支持链接、特殊文件或加密ZIP")
                if item.is_dir():
                    continue
                if mode == stat.S_IFDIR:
                    raise PackageError("ZIP文件类型冲突")
                total += item.file_size
                if item.file_size > MAX_FILE or total > MAX_TOTAL:
                    raise PackageError("解压资源超过大小上限")
                if item.file_size > max(1, item.compress_size) * 200:
                    raise PackageError("ZIP压缩比过高")
                with archive.open(item) as stream:
                    content = stream.read(MAX_FILE + 1)
                if len(content) != item.file_size or len(content) > MAX_FILE:
                    raise PackageError("ZIP资源大小不一致")
                files[name] = content
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        EOFError,
        zlib.error,
        UnicodeDecodeError,
    ) as exc:
        raise PackageError("ZIP损坏或压缩格式不支持") from exc
    roots = [name for name in files if PurePosixPath(name).name == "SKILL.md"]
    if len(roots) != 1:
        raise PackageError("需要且只能包含一个SKILL.md入口")
    root = posixpath.dirname(roots[0])
    prefix = root + "/" if root else ""
    outside = [name for name in files if not name.startswith(prefix)]
    normalized = {
        name[len(prefix) :]: content
        for name, content in files.items()
        if name.startswith(prefix)
    }
    errors = []
    notes = ["保存后可预览创作方案，确认后生成；包内命令不会执行。"]
    if outside:
        notes.append("根目录外文件未导入：" + ", ".join(outside))
    texts = {}
    assets = []
    for name, content in sorted(normalized.items()):
        ext = PurePosixPath(name).suffix.lower()
        mime = "application/octet-stream"
        if (
            ext in {".md", ".txt", ".yaml", ".yml", ".json"}
            or PurePosixPath(name).name.upper() in LICENSE_NAMES
        ):
            if len(content) > MAX_TEXT:
                raise PackageError("文本资源不得超过128 KiB")
            try:
                texts[name] = content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise PackageError("文本资源必须为UTF-8") from exc
            mime = "text/plain; charset=utf-8"
            if ext in {".yaml", ".yml", ".json"}:
                notes.append(f"{name}仅作为声明资料保存，不加载工具或执行配置。")
        elif ext in IMAGE_TYPES:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(content)) as img:
                        if (
                            img.format != IMAGE_TYPES[ext]
                            or img.width * img.height > 20_000_000
                            or getattr(img, "n_frames", 1) != 1
                        ):
                            raise PackageError("图片类型不匹配、像素过大或包含动画")
                        img.verify()
                mime = "image/" + ("jpeg" if ext in {".jpg", ".jpeg"} else ext[1:])
            except (
                OSError,
                SyntaxError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as exc:
                raise PackageError("图片资源无效") from exc
        else:
            errors.append(f"不支持文件类型：{name}（脚本、二进制和外部依赖不能执行）")
        assets.append(
            PackageAssetInfo(
                path=name,
                media_type=mime,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    instructions = texts.get("SKILL.md", "")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", instructions, re.S)
    if not match:
        raise PackageError("SKILL.md缺少YAML元数据头")
    meta = _yaml(match[1])
    name, description = meta.get("name"), meta.get("description")
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 64
        or not isinstance(description, str)
        or not 1 <= len(description.strip()) <= 4000
    ):
        raise PackageError("name应为1–64字，description应为1–4000字")
    refs = {}
    for path, text in texts.items():
        base = posixpath.dirname(path)
        if path == "agents/openai.yaml":
            config = _yaml(text)
            interface = config.get("interface") or {}
            if not isinstance(interface, dict):
                raise PackageError("agents/openai.yaml的interface必须为对象")
            targets = [
                interface[k]
                for k in ("icon_small", "icon_large")
                if isinstance(interface.get(k), str)
            ]
            base = ""  # Display asset paths are relative to the package root.
            if config.get("dependencies"):
                notes.append("包声明的工具或外部依赖不会安装、授权或执行。")
        elif path.lower().endswith(".md"):
            targets = re.findall(r"!?\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)", text)
            targets += re.findall(r"\[[^\]]+\]:\s*<?([^\s>]+)", text)
            targets += [
                v
                for v in re.findall(r"`([^`\n]+)`", text)
                if re.search(r"\.(?:md|txt|png|jpe?g|webp|ya?ml|json)$", v, re.I)
            ]
        else:
            continue
        resolved = []
        for target in targets:
            try:
                split = urlsplit(target)
            except ValueError:
                errors.append(f"引用格式无效：{path}")
                continue
            if split.scheme or split.netloc:
                notes.append(f"外部引用仅保留，不访问：{target}")
                continue
            target = unquote(split.path)
            if not target:
                continue
            if target.startswith("/") or "\\" in target or ":" in target:
                errors.append(f"引用越界：{path} → {target}")
                continue
            candidate = posixpath.normpath(posixpath.join(base, target))
            if (
                candidate.startswith("../")
                or candidate == ".."
                or candidate not in normalized
            ):
                errors.append(f"引用缺失或越界：{path} → {target}")
            else:
                resolved.append(candidate)
        refs[path] = sorted(set(resolved))
    canonical = [(a.path, a.sha256) for a in sorted(assets, key=lambda a: a.path)]
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode()
    ).hexdigest()
    cover = next((a for a in assets if a.media_type.startswith("image/")), None)
    license_value = meta.get("license")
    if license_value is None:
        license_value = next(
            (
                v
                for k, v in texts.items()
                if PurePosixPath(k).name.upper() in LICENSE_NAMES
            ),
            None,
        )
    report = PackageReport(
        name=name.strip(),
        description=description.strip(),
        root=root,
        content_sha256=digest,
        source=str(meta["source"])[:2000] if meta.get("source") else None,
        license=str(license_value)[:MAX_TEXT] if license_value is not None else None,
        assets=assets,
        references=refs,
        warnings=list(dict.fromkeys(notes)),
        errors=list(dict.fromkeys(errors)),
        can_import=not errors,
        cover_path=cover.path if cover else None,
        cover_data_url=(
            f"data:{cover.media_type};base64,"
            + base64.b64encode(normalized[cover.path]).decode()
        )
        if cover
        else None,
    )
    return ParsedPackage(report, normalized, instructions)
