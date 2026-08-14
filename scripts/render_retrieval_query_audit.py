"""Render query/ground-truth contact sheets for human label review."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    copy = image.convert("RGB")
    copy.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "#18181b")
    x = (width - copy.width) // 2
    y = (height - copy.height) // 2
    canvas.paste(copy, (x, y))
    return canvas


def render(
    queries_path: Path,
    manifest_path: Path,
    source_root: Path,
    output_dir: Path,
) -> list[Path]:
    queries = json.loads(queries_path.read_text(encoding="utf-8"))["queries"]
    images = {
        image["id"]: image
        for image in json.loads(manifest_path.read_text(encoding="utf-8"))["images"]
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    cell_w, cell_h = 560, 520
    columns, rows = 2, 5
    title_font, body_font, small_font = _font(22), _font(19), _font(16)

    for sheet_index, start in enumerate(range(0, len(queries), columns * rows), 1):
        page = Image.new("RGB", (cell_w * columns, cell_h * rows), "white")
        draw = ImageDraw.Draw(page)
        for offset, query in enumerate(queries[start : start + columns * rows]):
            col, row = offset % columns, offset // columns
            x0, y0 = col * cell_w, row * cell_h
            draw.rectangle((x0, y0, x0 + cell_w - 1, y0 + cell_h - 1), outline="#a1a1aa", width=2)
            draw.text((x0 + 12, y0 + 10), f"{query['id']} [{query['split']}]", font=title_font, fill="#111827")
            wrapped = textwrap.wrap(query["query"], width=24) or [query["query"]]
            draw.text((x0 + 12, y0 + 43), "\n".join(wrapped[:2]), font=body_font, fill="#111827", spacing=3)
            photo_ids = query.get("relevant_photo_ids", [])
            confuser_ids = query.get("confuser_photo_ids", [])
            display_ids = photo_ids or confuser_ids
            if not display_ids:
                draw.rectangle((x0 + 12, y0 + 105, x0 + cell_w - 12, y0 + cell_h - 42), fill="#f4f4f5")
                draw.text((x0 + 145, y0 + 280), "无结果负样本", font=title_font, fill="#52525b")
            else:
                image_top, image_h = y0 + 105, 355
                slot_w = (cell_w - 24 - 8 * (len(display_ids) - 1)) // len(display_ids)
                for idx, photo_id in enumerate(display_ids):
                    source = source_root / images[photo_id]["path"]
                    with Image.open(source) as image:
                        fitted = _fit(image, slot_w, image_h)
                    page.paste(fitted, (x0 + 12 + idx * (slot_w + 8), image_top))
                    draw.text(
                        (x0 + 16 + idx * (slot_w + 8), image_top + 8),
                        photo_id,
                        font=small_font,
                        fill="white",
                        stroke_width=2,
                        stroke_fill="black",
                    )
            forbidden = query.get("must_not_return", [])
            footer = "相关: " + (", ".join(photo_ids) if photo_ids else "[]")
            if confuser_ids:
                footer += " | 近邻非相关: " + ", ".join(confuser_ids)
            if forbidden:
                footer += " | 禁返: " + ", ".join(forbidden)
            draw.text((x0 + 12, y0 + cell_h - 36), footer, font=small_font, fill="#3f3f46")

        output = output_dir / f"retrieval-query-audit-{sheet_index}.jpg"
        page.save(output, quality=92)
        outputs.append(output)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description="Render retrieval-query audit sheets")
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    outputs = render(args.queries, args.manifest, args.source_root, args.output_dir)
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
