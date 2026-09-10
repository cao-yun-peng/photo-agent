"""Prepare full-corpus visual review assets without invoking a model."""
from pathlib import Path
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / '.project-to-act/tasks/S6-RETRIEVAL-20260908'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    evidence = TASK / 'evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'tests/eval/retrieval'
    corpus = json.loads((source / 'corpus.json').read_text(encoding='utf-8'))
    sources = [p for base in ('app', 'scripts/retrieval_eval', 'tests/eval/retrieval')
               for p in (ROOT / base).rglob('*')
               if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py', '.json', '.jsonl', '.tsv', '.csv')]
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(),
                'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_files': {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(sources)},
                'task_revision': 1, 'lifecycle_revision': 9,
                'conflict_check': 'Existing task records reviewed; no current independent agent writers. Existing source modifications preserved.'}
    (TASK / 'CONTEXT.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 20)
    for start in range(0, len(corpus), 16):
        sheet = Image.new('RGB', (1360, 1456), '#f4f4f4')
        draw = ImageDraw.Draw(sheet)
        for j, entry in enumerate(corpus[start:start+16]):
            with Image.open(ROOT / entry['path']) as im:
                thumb = ImageOps.contain(im.convert('RGB'), (332, 332))
            x, y = (j % 4)*340, (j // 4)*364
            sheet.paste(thumb, (x+(340-thumb.width)//2, y))
            draw.text((x+8,y+336), entry['photo_id'], fill='black', font=font)
        sheet.save(evidence / f'review-{start//16+1:02}.jpg', quality=92)
    queries = [json.loads(line) for line in (source/'queries.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    (evidence/'original-labels.json').write_text(json.dumps(queries,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'photos':len(corpus),'queries':len(queries),'sheets':(len(corpus)+15)//16,'context_files':len(sources)}))


if __name__ == '__main__':
    main()
