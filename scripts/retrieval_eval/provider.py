"""Auditable paid transport with durable, conservative CNY reservations.

Only the genuine provider transport is wrapped. No labels enter requests and no
provider outputs are replaced. A cancellation or missing usage keeps its reserve.
"""
import contextvars
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4
import httpx
from scripts.retrieval_eval.environment import EVIDENCE

CURRENT = contextvars.ContextVar('evaluation_query', default=('preflight','probe'))
CAP_MICRO = 30_000_000
PRICING = {'currency':'CNY','source':'https://help.aliyun.com/zh/model-studio/model-pricing',
           'checked_date':'2026-09-08','region':'Beijing','actual_bill_reconciled':False,
           'rates_per_million':{'text-embedding-v3':[0.5,0], 'qwen-plus':[0.8,8], 'qwen-vl-plus':[0.8,2]},
           'note':'qwen-plus output conservatively priced at thinking rate 8, versus documented non-thinking 2. No free quota/cache discounts assumed.'}

class MoneyCapExceeded(RuntimeError): pass

class Ledger:
    def __init__(self, path=None, cap_micro=CAP_MICRO):
        self.path = path or EVIDENCE/'provider-ledger.sqlite3'
        self.cap_micro = cap_micro
        self.exhausted = False
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, variant TEXT, query_id TEXT, stage TEXT, model TEXT, payload_sha256 TEXT, started_at TEXT, status TEXT, reserved_micro INTEGER, charged_micro INTEGER, input_tokens INTEGER, output_tokens INTEGER, latency_ms REAL, error_code TEXT, response_file TEXT)')
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=30)
        try:
            with db: yield db
        finally: db.close()
    def reserve(self, *, stage, model, payload_hash, amount):
        if type(amount) is not int or amount <= 0: raise ValueError('invalid_reservation_amount')
        cid = uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT coalesce(sum(charged_micro),0) FROM calls').fetchone()[0]
            if used+amount > self.cap_micro:
                self.exhausted = True
                raise MoneyCapExceeded('estimated_30_cny_cap')
            variant,qid = CURRENT.get()
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (cid,variant,qid,stage,model,payload_hash,datetime.now(timezone.utc).isoformat(),'reserved',amount,amount,None,None,None,None,None))
        return cid
    def finish(self,cid, *, status, elapsed, usage=None, rates=None,error=None,response_file=None):
        # Missing/failed/ambiguous usage is charged at the reserved upper estimate.
        ins = outs = None
        if isinstance(usage,dict):
            embedding = rates is not None and rates[1] == 0
            ins = usage.get('input_tokens',usage.get('prompt_tokens',usage.get('total_tokens') if embedding else None))
            outs = usage.get('output_tokens',usage.get('completion_tokens',0 if embedding else None))
        known = status == 'success' and type(ins) is int and type(outs) is int and ins >= 0 and outs >= 0
        with self.connect() as db:
            reserved = db.execute('SELECT reserved_micro FROM calls WHERE id=?',(cid,)).fetchone()[0]
            amount = round(ins*rates[0]+outs*rates[1]+0.499999) if known else reserved
            db.execute('UPDATE calls SET status=?,charged_micro=?,input_tokens=?,output_tokens=?,latency_ms=?,error_code=?,response_file=? WHERE id=?',
                       (status,amount,ins if known else None,outs if known else None,elapsed,error,response_file,cid))
        if amount > reserved: raise RuntimeError('reservation_underestimated_provider_usage')
    def summary(self,variant=None,query_id=None):
        where = []
        params = []
        for col,val in [('variant',variant),('query_id',query_id)]:
            if val is not None: where.append(col+'=?'); params.append(val)
        clause = ' WHERE '+' AND '.join(where) if where else ''
        with self.connect() as db:
            vals = db.execute("SELECT count(*),coalesce(sum(stage='visual'),0),coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),coalesce(sum(charged_micro),0),coalesce(sum(input_tokens IS NULL),0) FROM calls"+clause,params).fetchone()
        return dict(zip(['model_calls','visual_calls','input_tokens','output_tokens','cost_micro','calls_with_unknown_usage'],vals))

@contextmanager
def call_context(variant,query_id):
    token = CURRENT.set((variant,query_id))
    try: yield
    finally: CURRENT.reset(token)

def scrub_reasoning(value):
    if isinstance(value,dict): return {k:scrub_reasoning(v) for k,v in value.items() if k not in {'reasoning_content','reasoning','thoughts'}}
    if isinstance(value,list): return [scrub_reasoning(v) for v in value]
    return value

def reservation(payload):
    model = payload['model']
    if model not in PRICING['rates_per_million']: raise ValueError('unpriced_provider_model')
    images = 0
    def count(value):
        nonlocal images
        if isinstance(value,dict):
            total = 0
            for key,item in value.items():
                if key in {'image','image_url'}: images += 1; total += 16384
                else: total += count(item)+len(key)
            return total
        if isinstance(value,list): return sum(count(x) for x in value)
        return len(str(value).encode('utf8'))
    # UTF-8 bytes + per-message overhead upper-bounds ordinary text token counts.
    upper_in = count(payload)+1024
    if upper_in > 128000: raise ValueError('request_exceeds_frozen_pricing_tier')
    output = payload.get('max_tokens',payload.get('parameters',{}).get('max_tokens',0))
    if type(output) is not int or output < 0: raise ValueError('invalid_output_token_limit')
    if not model.startswith('text-embedding') and not output: raise ValueError('missing_output_token_limit')
    rates = PRICING['rates_per_million'][model]
    return max(1,int(upper_in*rates[0]+output*rates[1]+1)), rates, ('embedding' if model.startswith('text-embedding') else 'visual' if images else 'text')

@contextmanager
def metered_transport(ledger):
    original = httpx.AsyncClient.post
    async def post(client,url,*args,**kwargs):
        parsed_url = urlparse(str(url))
        if parsed_url.hostname != 'dashscope.aliyuncs.com' or parsed_url.scheme != 'https' or parsed_url.port not in (None,443):
            raise RuntimeError('evaluation_provider_host_not_allowlisted')
        payload = kwargs.get('json')
        if not isinstance(payload,dict): raise ValueError('unmetered_payload')
        amount,rates,stage = reservation(payload)
        cid = ledger.reserve(stage=stage,model=payload['model'],payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),amount=amount)
        started = time.monotonic()
        try:
            response = await original(client,url,*args,**kwargs)
        except BaseException as exc:
            ledger.finish(cid,status='ambiguous_failure',elapsed=(time.monotonic()-started)*1000,error=type(exc).__name__)
            raise
        try: data = response.json()
        except ValueError: data = {}
        dest = EVIDENCE/'provider-responses'/f'{cid}.json'
        dest.parent.mkdir(exist_ok=True)
        try:
            dest.write_text(json.dumps(scrub_reasoning(data),ensure_ascii=False)+'\n',encoding='utf8')
        except BaseException as exc:
            ledger.finish(cid,status='ambiguous_failure',elapsed=(time.monotonic()-started)*1000,error=type(exc).__name__)
            raise
        success = response.status_code==200 and isinstance(data,dict) and not data.get('code') and not data.get('error')
        ledger.finish(cid,status='success' if success else 'http_failure',elapsed=(time.monotonic()-started)*1000,
                      usage=data.get('usage') if isinstance(data,dict) else None,rates=rates,error=None if success else f'HTTP_{response.status_code}_provider_error',response_file=dest.name)
        return response
    httpx.AsyncClient.post = post
    try: yield
    finally: httpx.AsyncClient.post = original
