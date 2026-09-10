"""Freeze and run the real SearchService against isolated Postgres/Redis.

This is a production service-layer experiment, not a frontend or real HTTP
endpoint E2E claim. Only image transport/telemetry/scoring clock are adapted.
"""
import argparse
import asyncio
import base64
import contextvars
import hashlib
import importlib.metadata
import json
import logging
import mimetypes
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from scripts.retrieval_eval.environment import ROOT,TASK,EVIDENCE,DEV_USER,VALIDATION_USER,configure
from scripts.retrieval_eval.provider import Ledger,metered_transport,call_context,PRICING,MoneyCapExceeded

configure()
TRACE = contextvars.ContextVar('retrieval_evaluation_trace',default=None)
VARIANTS = {
    'A':{'verify_constraints':False,'verify_semantic':False,'visual':False},
    'B':{'verify_constraints':True,'verify_semantic':False,'visual':False},
    'C':{'verify_constraints':True,'verify_semantic':True,'visual':False},
    'D':{'verify_constraints':True,'verify_semantic':True,'visual':True},
}
DATASETS = {'development':'tests/eval/retrieval_v2','validation':'tests/eval/retrieval_validation'}
FREEZE = EVIDENCE/'freeze-v1.json'

def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=p.parent,suffix='.tmp',delete=False) as f:
            temporary = Path(f.name)
            f.write(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary,p)
    finally:
        if temporary and temporary.exists(): temporary.unlink()

def freeze():
    from app.config import settings
    if FREEZE.exists(): raise RuntimeError('freeze_exists_create_new_version_for_changes')
    config = {k:v for k,v in settings.model_dump().items() if k.startswith('search_') or k in {'qwen_vl_model','qwen_embedding_model','qwen_chat_model','dashscope_chat_url','task_cleanup_timeout_seconds'}}
    sources = sorted((ROOT/'app').rglob('*.py'))
    sources += sorted((ROOT/'scripts/retrieval_eval').glob('*.py'))
    sources += sorted((ROOT/'tests').glob('test_retrieval_*.py'))
    sources += [TASK/'PROTOCOL.md']
    sources += [ROOT/'requirements.txt',ROOT/'scripts/retrieval_eval/compose.yml']
    sources += sorted(p for p in (ROOT/DATASETS['development']).glob('*') if p.is_file())
    sources += [EVIDENCE/'development-index-snapshot.json']
    item = {'version':'retrieval-freeze-v1','frozen_at':datetime.now(timezone.utc).isoformat(),
            'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'worktree_dirty':True,'sources':{p.relative_to(ROOT).as_posix():digest(p) for p in sources},
            'runtime_versions':{'python':sys.version,'packages':{name:importlib.metadata.version(name) for name in ('SQLAlchemy','asyncpg','pgvector','redis','httpx','pydantic','pydantic-settings','numpy','Pillow')}},
            'app_source_file_set':sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'app').rglob('*.py')),
            'evaluation_source_file_set':sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'scripts/retrieval_eval').glob('*.py')),
            'settings':config,'variants':VARIANTS,'request':{'result_mode':'browse','limit':5,'auto_parse':False,'min_semantic_score':0.0,'w_semantic':0.7,'w_recency':0.2,'w_interaction':0.1},
            'concurrency':2,'query_order_seed':20260908,'scoring_time':'2026-09-08T00:00:00+00:00',
            'cache':'Private dataset+variant namespace; no cross-variant evidence reuse; first recorded attempt only. Cold/warm classification uses measured cache hits; no claim about upstream provider cache.',
            'inferred_types':'Production inference of photo type/selfie/people counts remains identical in A-D. A is current retrieval with verification disabled, not unconstrained vector-only search.',
            'transports':'Native DashScope HTTP. Visual input is original bytes via base64 instead of OSS signed URL. Preview URLs replaced by local placeholders. No photo content modification.',
            'model_pinning':'Existing aliases preserved; provider can update alias weights. Record resolved response model and timestamps; weights cannot be cryptographically frozen.',
            'pricing':PRICING}
    save(FREEZE,item)
    print(json.dumps({'freeze':str(FREEZE.relative_to(ROOT)),'files':len(sources),'sha256':digest(FREEZE)}))

def verify_freeze():
    data = json.loads(FREEZE.read_text(encoding='utf8'))
    changed = [name for name,h in data['sources'].items() if not (ROOT/name).exists() or digest(ROOT/name)!=h]
    current_app = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'app').rglob('*.py'))
    if current_app != data['app_source_file_set']: changed.append('app_file_set')
    current_eval = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT/'scripts/retrieval_eval').glob('*.py'))
    if current_eval != data['evaluation_source_file_set']: changed.append('evaluation_file_set')
    if changed: raise RuntimeError('frozen_files_changed:'+','.join(changed))
    return data

def canonical_index(rows):
    # Keep exact pgvector text and JSON evidence; normalize PostgreSQL's date
    # rendering, which differs from Python isoformat while preserving instants.
    fields = ('id','hash','oss_key','thumb_key','ai_description','ai_analysis','embedding','taken_at','status','photo_type','is_selfie','people_count','updated_at')
    result = []
    for row in rows:
        item = {k:row.get(k) for k in fields}
        for k in ('taken_at','updated_at'):
            if item[k]: item[k] = datetime.fromisoformat(str(item[k])).astimezone(timezone.utc).isoformat()
        if isinstance(item['embedding'],str): item['embedding'] = json.loads(item['embedding'])
        result.append(item)
    return sorted(result,key=lambda r:r['id'])

async def verify_database(dataset,user,session_factory):
    from sqlalchemy import text
    expected_path = EVIDENCE/('development-index-snapshot.json' if dataset=='development' else 'validation-index-snapshot.json')
    expected = canonical_index(json.loads(expected_path.read_text(encoding='utf8')))
    async with session_factory() as db:
        actual = await db.scalar(text('SELECT coalesce(json_agg(row_to_json(p)),\'[]\'::json) FROM photos p WHERE user_id=CAST(:u AS uuid)'),{'u':user})
        profiles = await db.scalar(text('SELECT count(*) FROM user_profiles WHERE user_id=CAST(:u AS uuid)'),{'u':user})
        if profiles or canonical_index(actual)!=expected: raise RuntimeError('isolated_database_drift')

def existing_result(path,query_id,variant,dataset,freeze_hash):
    if not path.exists(): return False
    data = json.loads(path.read_text(encoding='utf8'))
    expected = {'query_id':query_id,'variant':variant,'dataset':dataset,'freeze_sha256':freeze_hash}
    if any(data.get(k)!=v for k,v in expected.items()): raise RuntimeError('existing_result_identity_mismatch')
    if not isinstance(data.get('returned_photo_ids'),list) or 'completed_at' not in data: raise RuntimeError('incomplete_existing_result')
    return True

def load_dataset(name):
    root = ROOT/DATASETS[name]
    corpus = json.loads((root/'corpus.json').read_text(encoding='utf8'))
    queries = [json.loads(l) for l in (root/'queries.jsonl').read_text(encoding='utf8').splitlines() if l.strip()]
    assert len({x['id'] for x in queries}) == len(queries)
    for p in corpus: assert digest(ROOT/p['path']) == p['sha256'], 'image_changed'
    return corpus,queries

def image_input(key,**kwargs):
    if not key.startswith('eval-local:'): raise ValueError('unexpected_image_key')
    p = (ROOT/key.removeprefix('eval-local:')).resolve()
    if not p.is_relative_to(ROOT): raise ValueError('image_path_outside_workspace')
    return 'data:'+str(mimetypes.guess_type(p)[0] or 'image/jpeg')+';base64,'+base64.b64encode(p.read_bytes()).decode()

def instrument(short_ids):
    import app.services.search_engine as engine_module
    import app.services.search_verification as verification
    import app.services.search_visual_verifier as visual
    engine_module.sign_get_url = lambda key,**kwargs:'eval-preview:'+key
    visual.sign_get_url = image_input
    original_text = verification.judge_candidate_evidence
    original_visual = verification.judge_visual_candidates
    async def text_judge(q,candidates,**kwargs):
        decisions,meta = await original_text(q,candidates,**kwargs)
        keys = {x['candidate_key']:short_ids[x['photo_id']] for x in candidates}
        TRACE.get().append({'stage':'text','candidates':keys,'decisions':[x.as_dict() for x in decisions],'meta':meta})
        return decisions,meta
    async def visual_judge(q,candidates,**kwargs):
        decisions,meta = await original_visual(q,candidates,**kwargs)
        keys = {x.candidate_key:short_ids[x.photo_id] for x in candidates}
        TRACE.get().append({'stage':'visual','candidates':keys,'decisions':[x.as_dict() for x in decisions],'meta':meta})
        return decisions,meta
    verification.judge_candidate_evidence = text_judge
    verification.judge_visual_candidates = visual_judge

async def run(dataset,variant):
    frozen = verify_freeze()
    approval = json.loads((EVIDENCE/'outbound-approval.json').read_text())
    if approval['status'] != 'authorized': raise RuntimeError('explicit_data_egress_approval_pending')
    from app.config import settings
    from app.database import AsyncSessionLocal,engine
    from app.services.search_engine import SearchService
    from app.services.search_contracts import SearchRequest
    from app.services.search_store import SearchStore
    from app.services.lock import get_redis
    corpus,queries = load_dataset(dataset)
    if dataset == 'validation':
        vf = EVIDENCE/'validation-freeze.json'
        if not vf.exists(): raise RuntimeError('validation_not_frozen')
        data = json.loads(vf.read_text())
        for name,h in data['sources'].items():
            if digest(ROOT/name)!=h: raise RuntimeError('validation_changed')
        if variant not in {'A',data['selected_variant']}: raise RuntimeError('validation_variant_not_selected')
    for k,v in frozen['settings'].items(): setattr(settings,k,v)
    settings.search_visual_verify_enabled = VARIANTS[variant]['visual']
    settings.search_cache_revision += ':'+dataset+':'+variant
    user = DEV_USER if dataset=='development' else VALIDATION_USER
    await verify_database(dataset,user,AsyncSessionLocal)
    order = list(queries)
    random.Random(frozen['query_order_seed']).shuffle(order)
    short_ids = {p['database_photo_id']:p['photo_id'] for p in corpus}
    instrument(short_ids)
    out = EVIDENCE/'runs'/dataset/variant
    out.mkdir(parents=True,exist_ok=True)
    schedule = {'dataset':dataset,'variant':variant,'freeze_sha256':digest(FREEZE),'query_ids':[q['id'] for q in order]}
    if (out/'schedule.json').exists():
        assert json.loads((out/'schedule.json').read_text())==schedule
    else: save(out/'schedule.json',schedule)
    ledger = Ledger()
    redis = await get_redis()
    await redis.ping()
    semaphore = asyncio.Semaphore(frozen['concurrency'])
    completed = sum((out/f"{q['id']}.json").exists() for q in order)
    async def one(q):
        nonlocal completed
        dest = out/f"{q['id']}.json"
        if existing_result(dest,q['id'],variant,dataset,digest(FREEZE)): return
        async with semaphore:
            if ledger.exhausted:
                save(dest,{'query_id':q['id'],'variant':variant,'dataset':dataset,'freeze_sha256':digest(FREEZE),
                           'attempted':False,'returned_photo_ids':[],'success':False,'error_code':'estimated_money_cap',
                           'stop_reason':'budget_exhausted','latency_ms':0.0,'model_calls':0,'visual_calls':0,
                           'input_tokens':0,'output_tokens':0,'estimated_cost_cny':0.0,
                           'completed_at':datetime.now(timezone.utc).isoformat()})
                return
            # Never silently rerun an interrupted query that may have paid calls.
            marker = out/f"{q['id']}.started.json"
            if marker.exists(): raise RuntimeError('interrupted_query_requires_explicit_accounting:'+q['id'])
            save(marker,{'query_id':q['id'],'started_at':datetime.now(timezone.utc).isoformat()})
            trace = []
            token = TRACE.set(trace)
            result = {}
            error = None
            started = time.monotonic()
            with call_context(dataset+'-'+variant,q['id']):
                try:
                    # Only actual user query enters SUT; labels are never passed.
                    request = SearchRequest(q=q['query'],**frozen['request'],verify_constraints=VARIANTS[variant]['verify_constraints'],verify_semantic=VARIANTS[variant]['verify_semantic'])
                    async with AsyncSessionLocal() as db:
                        service = SearchService(db,user)
                        plan = await service.create_plan(request)
                        plan = plan.model_copy(update={'scoring_time':frozen['scoring_time']})
                        store = SearchStore(redis)
                        async with store.mutation(user,plan.id) as snap: snap['plan'] = plan.model_dump(mode='json')
                        result = await service.search(request,plan_id=plan.id)
                        snapshot = await store.load(user,plan.id)
                        result['evaluation_snapshot'] = snapshot
                except Exception as exc:
                    error = getattr(exc,'code',type(exc).__name__)
                finally: TRACE.reset(token)
            telemetry = ledger.summary(dataset+'-'+variant,q['id'])
            raw_ids = [str(p.get('id')) for p in result.get('items',[])]
            unknown_ids = [pid for pid in raw_ids if pid not in short_ids]
            returned = [short_ids[pid] for pid in raw_ids if pid in short_ids]
            if unknown_ids: error = 'unknown_returned_photo_id'
            snapshot = result.pop('evaluation_snapshot',{})
            snapshot.pop('plan',None)
            safe_result = {k:v for k,v in result.items() if k not in {'items','next_cursor','_search_plan_id'}}
            degraded = bool((result.get('rerank_check') or {}).get('degraded'))
            row = {'query_id':q['id'],'variant':variant,'dataset':dataset,'freeze_sha256':digest(FREEZE),'returned_photo_ids':returned,
                   'attempted':True,'invalid_returned_database_ids':unknown_ids,
                   'success':error is None and not degraded,'error_code':error,
                   'stop_reason':result.get('stop_reason',error or 'unknown'),
                   'latency_ms':round((time.monotonic()-started)*1000,2),
                   **{k:telemetry[k] for k in ('model_calls','visual_calls','input_tokens','output_tokens','calls_with_unknown_usage')},
                   'estimated_cost_cny':telemetry['cost_micro']/1_000_000,
                   'result_meta':safe_result,'verification_trace':trace,'snapshot':snapshot,
                   'completed_at':datetime.now(timezone.utc).isoformat()}
            save(dest,row)
            completed += 1
            print(json.dumps({'dataset':dataset,'variant':variant,'completed':completed,'total':len(order),'query_id':q['id'],'returned_count':len(returned),'stop_reason':row['stop_reason'],'estimated_total_cny':ledger.summary()['cost_micro']/1_000_000}),flush=True)
    try:
        with metered_transport(ledger):
            outcomes = await asyncio.gather(*(one(q) for q in order),return_exceptions=True)
        failures = [type(x).__name__ for x in outcomes if isinstance(x,BaseException)]
        if failures:
            save(out/'harness-failures.json',{'error_types':failures,'needs_accounting':True})
            raise RuntimeError('harness_failures_preserved_without_implicit_reruns')
    finally:
        await engine.dispose()
        await redis.aclose()

def main():
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['freeze','verify','run'])
    parser.add_argument('--dataset',choices=DATASETS,default='development')
    parser.add_argument('--variant',choices=VARIANTS,default='A')
    args = parser.parse_args()
    if args.action == 'freeze': freeze()
    elif args.action == 'verify': print(json.dumps({'verified':verify_freeze()['version']}))
    else: asyncio.run(run(args.dataset,args.variant))

if __name__ == '__main__': main()
