"""Read only the manifest's development photos; seed a separate database."""
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime
from uuid import UUID
from scripts.retrieval_eval.environment import ROOT, EVIDENCE, DEV_USER, configure

configure()

async def main():
    from sqlalchemy import text, select
    from app.database import engine, AsyncSessionLocal, Base
    from app.models import Photo, User
    corpus = json.loads((ROOT/'tests/eval/retrieval_v2/corpus.json').read_text())
    export_path = EVIDENCE/'development-index-snapshot.json'
    if not export_path.exists():
        sql = "BEGIN TRANSACTION READ ONLY; SELECT coalesce(json_agg(row_to_json(p)), '[]'::json) FROM photos p WHERE user_id = '"+DEV_USER+"'; COMMIT;"
        raw = subprocess.check_output(['docker','exec','photo-agent-db','psql','-U','postgres','-d','photo_agent','-At','-c',sql])
        lines = raw.decode('utf8').splitlines()
        records = json.loads(next(x for x in lines if x.startswith('[')))
        by_id = {str(x['id']):x for x in records}
        selected = []
        for p in corpus:
            row = by_id[p['database_photo_id']]
            assert row['hash'] == p['sha256'], 'Source corpus changed'
            # Do not preserve external object locations in portable evidence.
            row['oss_key'] = 'eval-local:'+p['path']
            row['thumb_key'] = row['oss_key']
            selected.append(row)
        export_path.write_text(json.dumps(selected,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    records = json.loads(export_path.read_text(encoding='utf8'))
    assert len(records) == len(corpus) == 137
    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        user = await db.get(User,UUID(DEV_USER))
        if not user:
            db.add(User(id=UUID(DEV_USER),wechat_openid='retrieval-eval-development-20260908',nickname='Isolated retrieval evaluation'))
            await db.flush()
        for r in records:
            if await db.get(Photo,UUID(r['id'])): continue
            for col in Photo.__table__.columns:
                if r.get(col.name) is None: continue
                if str(col.type) == 'UUID': r[col.name] = UUID(r[col.name])
                elif 'DATETIME' in str(col.type).upper(): r[col.name] = datetime.fromisoformat(r[col.name])
            if isinstance(r.get('embedding'),str): r['embedding'] = json.loads(r['embedding'])
            assert len(r['embedding']) == 1024
            db.add(Photo(**r))
        await db.commit()
        count = await db.scalar(text('SELECT count(*) FROM photos'))
    await engine.dispose()
    print(json.dumps({'isolated_photo_count':count,'snapshot_sha256':hashlib.sha256(export_path.read_bytes()).hexdigest(),'source_access':'read_only'}))

if __name__ == '__main__': asyncio.run(main())
