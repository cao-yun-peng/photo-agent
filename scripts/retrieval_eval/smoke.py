"""Offline integration smoke: real SQL/Redis, fixed vector, no provider allowed.

This fixture is never a retrieval quality sample and produces no accuracy score.
"""
import asyncio
import json
import math
from unittest.mock import patch
from uuid import uuid4
from scripts.retrieval_eval.environment import configure,DEV_USER,EVIDENCE
from scripts.retrieval_eval.run import save,verify_database
configure()

async def main():
    from app.config import settings
    from app.database import AsyncSessionLocal,engine
    from app.services.lock import get_redis
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest
    settings.search_cache_revision = 'offline-smoke-'+uuid4().hex
    await verify_database('development',DEV_USER,AsyncSessionLocal)
    rows = json.loads((EVIDENCE/'development-index-snapshot.json').read_text(encoding='utf8'))
    vectors = {r['id']:json.loads(r['embedding']) if isinstance(r['embedding'],str) else r['embedding'] for r in rows}
    vector = vectors[rows[0]['id']]
    norm = lambda v:math.sqrt(sum(x*x for x in v))
    cos = lambda v:sum(a*b for a,b in zip(v,vector))/(norm(v)*norm(vector))
    expected = sorted(vectors,key=lambda key:(-cos(vectors[key]),key))[:5]
    async def fixed_embedding(_): return vector,False
    async def forbidden(*args,**kwargs): raise AssertionError('external_call_forbidden_in_offline_smoke')
    with patch('app.services.search_engine.get_query_embedding',fixed_embedding),patch('app.services.search_engine.sign_get_url',lambda key,**kw:'offline-preview'),patch('httpx.AsyncClient.post',forbidden):
        async with AsyncSessionLocal() as db:
            result = await SearchService(db,DEV_USER).search(SearchRequest(q='isolated integration fixture',limit=5,auto_parse=False,verify_constraints=False,verify_semantic=False,min_semantic_score=0.0))
    actual = [p['id'] for p in result['items']]
    assert actual == expected, 'SQL rank differs from independent cosine reference'
    assert result['search_usage']['calls']==0, 'Smoke attempted model call'
    assert len(actual)==len(set(actual))==5
    record = {'success':True,'scope':'Real isolated SearchService + SQL pgvector + Redis; fixed known vector; all external POST blocked.',
              'assertions':['Top-5 exact rank agrees with independent Python cosine computation','5 unique existing photos returned','0 model calls'],
              'not_accuracy_evaluation':True,'provider_calls':0,'stop_reason':result['stop_reason']}
    save(EVIDENCE/'offline-integration-smoke.json',record)
    await engine.dispose()
    await (await get_redis()).aclose()
    print(json.dumps(record))

if __name__=='__main__': asyncio.run(main())
