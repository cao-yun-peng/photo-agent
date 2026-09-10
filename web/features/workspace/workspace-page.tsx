/* eslint-disable @next/next/no-img-element -- Private signed photo previews. */
'use client';
import { useRef, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AuthGate } from '@/components/auth-gate';
import { AppShell } from '@/components/app-shell';
import { getWorkspace, updateWorkspace, undoWorkspace, type Workspace, type WorkspaceCommand, type WorkspaceAction } from '@/lib/api/workspace';
import { createIdempotencyKey } from '@/lib/api/generations';
import { resolveMediaUrl } from '@/lib/api/media-url';
import styles from './workspace-page.module.css';

export function WorkspacePageView() { return <AuthGate>{user => <AppShell user={user}><WorkspaceView /></AppShell>}</AuthGate>; }
export function WorkspaceView() {
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ['workspace'], queryFn: getWorkspace });
  const [report, setReport] = useState<string[]>([]);
  const receipt = useRef<{ signature: string; key: string } | null>(null);
  const accept = (result: WorkspaceAction) => {
    queryClient.setQueryData(['workspace'], result.workspace);
    const details = result.report as Record<string, unknown> | null;
    setReport(Array.isArray(details?.issues) ? details.issues.filter((x): x is string => typeof x === 'string') : []);
  };
  const change = useMutation({ mutationFn: (command: WorkspaceCommand) => {
    if (!query.data) throw new Error('请先加载选片区');
    const signature = JSON.stringify([query.data.revision, command]);
    if (receipt.current?.signature !== signature) receipt.current = { signature, key: createIdempotencyKey() };
    return updateWorkspace(command, query.data.revision, receipt.current.key);
  }, onSuccess: accept, onError: () => { void queryClient.invalidateQueries({ queryKey: ['workspace'] }); } });
  const undo = useMutation({ mutationFn: () => {
    if (!query.data?.undo) throw new Error('当前没有可撤销操作');
    return undoWorkspace(query.data.undo.operation_id, query.data.revision);
  }, onSuccess: accept, onError: () => { void queryClient.invalidateQueries({ queryKey: ['workspace'] }); } });
  const candidates = (params.get('candidates') || '').split(',').filter(x => /^[0-9a-f-]{36}$/i.test(x)).slice(0,100);
  const persisted = query.data?.selection_report as Record<string, unknown> | null | undefined;
  const issues = report.length ? report : Array.isArray(persisted?.issues) ? persisted.issues.filter((x): x is string => typeof x === 'string') : [];
  const error = query.error || change.error || undo.error;
  return <div className={styles.page}>
    <header className={styles.header}><div><h1>选片与相册</h1><p>已选照片会保留，搜索换页不会清空。</p></div><Link href="/search">返回搜索找照片</Link></header>
    {error ? <p role="alert" className={styles.error}>{error.message}</p> : null}
    {query.data ? <><div className={styles.controls}><span>已选 {query.data.selection.length} 张</span><button type="button" disabled={!query.data.undo || undo.isPending || change.isPending} onClick={() => undo.mutate()}>撤销上一步</button><span className={styles.note}>10 分钟内可撤销；后续已有修改时会拒绝覆盖。</span></div>
      {query.data.missing_selection_count ? <p>部分照片已不可用，已从展示中排除。</p> : null}
      <WorkspaceEditor key={query.data.revision} workspace={query.data} candidates={candidates} busy={change.isPending || undo.isPending} change={command => change.mutate(command)} />
      {issues.length ? <ul role="status">{issues.map(x => <li key={x}>{x}</li>)}</ul> : null}
    </> : <p>正在读取选片区…</p>}
  </div>;
}
function WorkspaceEditor({ workspace, candidates, busy, change }: { workspace: Workspace; candidates: string[]; busy: boolean; change: (command: WorkspaceCommand) => void }) {
  const [task, setTask] = useState(workspace.task);
  const [preferences, setPreferences] = useState(workspace.preferences);
  const [title, setTitle] = useState('');
  const [fact, setFact] = useState('');
  const [factId, setFactId] = useState(workspace.selection[0]?.id || '');
  const facts = workspace.facts as unknown as { photo_id: string; value: string; active: boolean; current_version: string }[];
  const pool = candidates.length ? candidates : workspace.selection.map(p => p.id);
  return <>
    <section className={styles.panel}><h2>本次选片目标</h2><div className={styles.controls}>
      <label>目标说明<input aria-label="目标说明" maxLength={200} value={task.goal || ''} onChange={e => setTask({ ...task, goal: e.target.value })} /></label>
      <label>目标张数<input aria-label="目标张数" type="number" min={1} max={100} value={task.target_count} onChange={e => setTask({ ...task, target_count: Number(e.target.value) })} /></label>
      <label>至少合照<input aria-label="至少合照" type="number" min={0} max={100} value={task.min_group_count} onChange={e => setTask({ ...task, min_group_count: Number(e.target.value) })} /></label>
      <label><span>本次优先风景</span><input aria-label="本次优先风景" type="checkbox" checked={Boolean(task.prefer_landscape)} onChange={e => setTask({ ...task, prefer_landscape: e.target.checked })} /></label>
      <button type="button" disabled={busy} onClick={() => change({ kind: 'set_task', task })}>保存本次目标</button>
      <button type="button" disabled={busy || !pool.length} onClick={() => change({ kind: 'curate', photo_ids: pool })}>按已保存目标筛选 {pool.length} 张候选</button>
    </div><p className={styles.note}>先保存目标再筛选。优先固定照片、合照数量和场景覆盖；比较可用缩略图识别近似重复；固定照片会保留，结果仍需人工核对。</p></section>
    <section className={styles.panel}><h2>稳定选片区</h2><div className={styles.controls}><button type="button" disabled={busy || !workspace.selection.length} onClick={() => change({ kind: 'clear_selection' })}>清空选片与本次目标</button><label>相册名称<input aria-label="相册名称" maxLength={80} value={title} onChange={e => setTitle(e.target.value)} /></label><button type="button" disabled={busy || !title.trim() || !workspace.selection.length} onClick={() => change({ kind: 'save_album', title: title.trim() })}>保存为相册</button></div>
    <div className={styles.grid}>{workspace.selection.map((photo, index) => <article className={styles.card} key={photo.id}><strong>已选 {index + 1}</strong>{resolveMediaUrl(photo.thumb_url) ? <img src={resolveMediaUrl(photo.thumb_url)!} alt={photo.description || '已选照片'} /> : null}<p>{photo.description || '照片'}</p>{photo.correction ? <p>明确修正：{String(photo.correction.value)}</p> : null}{photo.correction_stale ? <p>照片已变化，旧修正不再作为有效记忆。</p> : null}<div><button type="button" disabled={busy} onClick={() => change({ kind: 'set_task', task: { ...workspace.task, locked_ids: workspace.task.locked_ids?.includes(photo.id) ? workspace.task.locked_ids.filter(id => id !== photo.id) : [...(workspace.task.locked_ids || []),photo.id] } })}>{workspace.task.locked_ids?.includes(photo.id) ? '取消固定' : '固定这张'}</button><button type="button" disabled={busy} aria-label={`移除已选第 ${index+1} 张`} onClick={() => change({ kind: 'remove_selection', photo_ids: [photo.id] })}>移除</button></div></article>)}</div></section>
    <section className={styles.panel}><h2>我的相册</h2><div className={styles.albums}>{workspace.albums.map(album => <article key={album.id}><strong>{album.title}</strong><span>{album.count} 张</span><button type="button" disabled={busy} onClick={() => change({ kind: 'load_album', album_id: album.id })}>载入选片区</button><button type="button" disabled={busy || !workspace.selection.length} onClick={() => change({ kind: 'save_album', album_id: album.id, title: album.title })}>用当前选片更新</button><button type="button" disabled={busy} onClick={() => change({ kind: 'delete_album', album_id: album.id })}>移除相册</button></article>)}</div><p className={styles.note}>移除相册不会删除原照片。</p></section>
    <section className={styles.panel}><h2>明确偏好</h2><div className={styles.controls}><label>常用选片偏好<select aria-label="常用选片偏好" value={preferences.preferred_subject} onChange={e => setPreferences({ ...preferences, preferred_subject: e.target.value as 'none'|'people'|'landscape' })}><option value="none">无固定偏好</option><option value="people">优先人物</option><option value="landscape">优先风景</option></select></label><label>常用标题要求<select aria-label="常用标题要求" value={preferences.title_mode} onChange={e => setPreferences({ ...preferences, title_mode: e.target.value as 'auto'|'none' })}><option value="auto">自动</option><option value="none">无文字</option></select></label><button type="button" disabled={busy} onClick={() => change({ kind: 'set_preferences', preferences })}>保存明确偏好</button><button type="button" disabled={busy} onClick={() => change({ kind: 'set_preferences', preferences: { preferred_subject: 'none', title_mode: 'auto' } })}>清除明确偏好</button></div><p className={styles.note}>仅记住你主动保存的内容；本次要求优先。Agent 可按需读取，点击或拒绝照片不会自动写入长期偏好。</p></section>
    <section className={styles.panel}><h2>照片事实修正</h2><div className={styles.controls}><label>照片<select aria-label="修正照片" value={factId} onChange={e => setFactId(e.target.value)}>{workspace.selection.map((p,i) => <option value={p.id} key={p.id}>已选 {i+1} · {p.description || p.id.slice(0,8)}</option>)}</select></label><label>正确事实<input aria-label="正确事实" maxLength={160} value={fact} onChange={e => setFact(e.target.value)} placeholder="例如：这是杭州，不是苏州" /></label><button type="button" disabled={busy || !fact.trim() || !factId} onClick={() => change({ kind: 'set_fact', photo_ids: [factId], photo_version: workspace.selection.find(p => p.id === factId)?.version, fact: fact.trim() })}>保存事实修正</button></div><ul>{facts.map(item => <li key={item.photo_id}>{item.value} · {item.active ? '当前照片版本有效' : '照片已变化，待重新核对'} <button type="button" disabled={busy} onClick={() => change({ kind: 'clear_fact', photo_ids: [item.photo_id], photo_version: item.current_version })}>删除此修正</button></li>)}</ul><button type="button" disabled={busy} onClick={() => change({ kind: 'clear_facts' })}>清除全部事实修正</button><p className={styles.note}>作为明确记忆供 Agent 读取，不改写原始照片分析或搜索索引。</p></section>
  </>;
}
