"""Force all evaluation writes to the dedicated local services before app imports."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / '.project-to-act/tasks/S6-RETRIEVAL-20260908'
EVIDENCE = TASK / 'evidence'
DEV_USER = '0ba7ad31-c0eb-4483-8ede-037833819759'
VALIDATION_USER = '63a13a95-78ad-57b0-8047-173bec57781b'

def configure():
    os.environ.update({
        'DATABASE_URL':'postgresql+asyncpg://retrieval_eval:retrieval_eval_local_only@127.0.0.1:55449/photo_agent_retrieval_eval_test',
        'REDIS_URL':'redis://127.0.0.1:56399/15',
        'OTEL_SDK_DISABLED':'true',
        'APP_ENV':'evaluation',
        'OTEL_ENABLED':'false',
        'SEARCH_CACHE_REVISION':'retrieval-eval-20260908-v1',
    })
