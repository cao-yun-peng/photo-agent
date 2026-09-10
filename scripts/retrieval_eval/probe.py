"""Small paid connectivity probes; excluded from all retrieval scores."""
import asyncio
import base64
import json
import httpx
from scripts.retrieval_eval.environment import configure,ROOT,EVIDENCE
from scripts.retrieval_eval.provider import Ledger,metered_transport,call_context,PRICING
configure()

async def main():
    approval = json.loads((EVIDENCE/'outbound-approval.json').read_text(encoding='utf8'))
    if approval.get('status') != 'authorized':
        raise RuntimeError('explicit_data_egress_approval_pending')
    from app.config import settings
    from app.services.ai import embed_query, describe_image
    ledger = Ledger()
    checks = []
    async def embedding(): return len(await embed_query('连接检查：照片检索')) == 1024
    async def chat():
        async with httpx.AsyncClient(timeout=30,trust_env=False) as c:
            r = await c.post(settings.dashscope_chat_url,headers={'Authorization':'Bearer '+settings.dashscope_api_key},json={'model':settings.qwen_chat_model,'messages':[{'role':'user','content':'只回复 OK'}],'max_tokens':16,'temperature':0})
            r.raise_for_status()
            return bool(r.json()['choices'][0]['message']['content'])
    async def vision():
        url = 'data:image/jpeg;base64,'+base64.b64encode((ROOT/'test_photos/p-004_ramen.jpg').read_bytes()).decode()
        return bool(await describe_image(url))
    with metered_transport(ledger):
        for name,fn in [('embedding',embedding),('chat',chat),('vision',vision)]:
            with call_context('preflight',name):
                try: ok = await fn(); row={'stage':name,'success':ok}
                except Exception as exc: row={'stage':name,'success':False,'error_type':type(exc).__name__}
                checks.append(row); print(json.dumps(row),flush=True)
    (EVIDENCE/'pricing.json').write_text(json.dumps(PRICING,indent=2)+'\n',encoding='utf8')
    (EVIDENCE/'preflight.json').write_text(json.dumps({'checks':checks,'ledger':ledger.summary()},indent=2)+'\n',encoding='utf8')
    print(json.dumps(ledger.summary()))

if __name__ == '__main__': asyncio.run(main())
