"""Bind independent frozen labels/index to the development-selected policy."""
import json
from scripts.retrieval_eval.environment import ROOT,EVIDENCE
from scripts.retrieval_eval.run import digest,save,verify_freeze,FREEZE

def main():
    verify_freeze()
    dest = EVIDENCE/'validation-freeze.json'
    if dest.exists(): raise RuntimeError('validation_freeze_already_exists')
    selection = json.loads((EVIDENCE/'report/selection.json').read_text(encoding='utf8'))
    # Report action is required to reject missing/unattempted development rows.
    if not selection.get('selected_variant') or selection.get('status') == 'incomplete':
        raise RuntimeError('development_selection_incomplete')
    if selection.get('freeze_sha256') != digest(FREEZE): raise RuntimeError('selection_freeze_mismatch')
    labels = ROOT/'tests/eval/retrieval_validation/freeze.json'
    manifest = json.loads(labels.read_text(encoding='utf8'))
    for name,h in manifest['files'].items():
        if digest(ROOT/name)!=h: raise RuntimeError('validation_labels_changed')
    index = EVIDENCE/'validation-index-snapshot.json'
    records = json.loads(index.read_text(encoding='utf8'))
    corpus = json.loads((ROOT/'tests/eval/retrieval_validation/corpus.json').read_text())
    if {r['id'] for r in records}!={p['database_photo_id'] for p in corpus}: raise RuntimeError('validation_index_rows_missing')
    sources = {**manifest['files'],labels.relative_to(ROOT).as_posix():digest(labels),index.relative_to(ROOT).as_posix():digest(index)}
    item = {'version':'independent-validation-execution-v1','selected_variant':selection['selected_variant'],
            'baseline_variant':'A','development_freeze_sha256':digest(FREEZE),'sources':sources,
            'selection_sha256':digest(EVIDENCE/'report/selection.json'),
            'index_photos':len(records),'indexed_photos':sum(bool(r.get('embedding')) for r in records),
            'no_tuning_after_validation':True}
    save(dest,item)
    print(json.dumps({'validation_execution_frozen':True,'selected_variant':item['selected_variant'],'photos':len(records)}))

if __name__=='__main__': main()
