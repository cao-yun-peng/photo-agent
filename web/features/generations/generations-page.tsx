/* eslint-disable @next/next/no-img-element -- Results use short-lived signed backend URLs. */
'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { listGenerations, shouldPollGeneration, type Generation } from '@/lib/api/generations';
import { resolveMediaUrl } from '@/lib/api/media-url';
import styles from './generations-page.module.css';

const STATUS_LABELS: Record<string, string> = { awaiting_confirmation: '待确认', pending: '排队中', processing: '生成中', queue_failed: '入队失败', done: '已完成', failed: '失败' };
function formatTime(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false }); }

export function GenerationsPageView() { return <AuthGate>{(user) => <AppShell user={user}><GenerationsHistory /></AppShell>}</AuthGate>; }

function GenerationsHistory() {
  const [selected, setSelected] = useState<Generation | null>(null);
  const history = useQuery({ queryKey: ['generations', 'history'], queryFn: () => listGenerations({ limit: 60 }), refetchInterval: (query) => query.state.data?.some((item) => shouldPollGeneration(item.status)) ? 4_000 : false });
  useEffect(() => { if (!selected) return; const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelected(null); }; window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close); }, [selected]);
  const failure = history.error as ApiFailure | null;
  const items = history.data || [];
  return <>
    <header className={styles.header}><div><p>Creation archive</p><h1>生成历史</h1><span>{history.isPending ? '正在读取任务…' : `共 ${items.length} 条创作记录`}</span></div><div><button type="button" disabled={history.isFetching} onClick={() => void history.refetch()}>{history.isFetching ? '刷新中…' : '刷新'}</button><Link href="/skills">开始新创作</Link></div></header>
    {failure ? <div className={styles.error} role="alert">{failure.detail || '生成历史加载失败'}<button type="button" onClick={() => void history.refetch()}>重试</button></div> : null}
    {!history.isPending && !failure && !items.length ? <section className={styles.empty}><span>✦</span><h2>还没有生成记录</h2><p>从 Skill 广场挑选配方，用相册照片创作第一张新作品。</p><Link href="/skills">浏览 Skill 广场</Link></section> : null}
    {items.length ? <section className={styles.grid} aria-label="生成任务历史">{items.map((item) => { const result = resolveMediaUrl(item.result_url); return <article className={styles.card} key={item.id} data-status={item.status}><button type="button" className={styles.visual} onClick={() => setSelected(item)}>{result ? <img src={result} alt="生成结果" loading="lazy" /> : <div><span className={shouldPollGeneration(item.status) ? 'status-dot' : undefined}>✦</span><small>{STATUS_LABELS[item.status] || item.status}</small></div>}<em>{STATUS_LABELS[item.status] || item.status}</em></button><div className={styles.body}><div><strong>{item.model}</strong><span>{formatTime(item.created_at)}</span></div><p>{item.error_message || item.extra_prompt || '使用 Skill 创建的照片作品'}</p><dl><div><dt>费用</dt><dd>¥ {item.cost_yuan}</dd></div><div><dt>尝试</dt><dd>{item.attempt_count} 次</dd></div></dl><div className={styles.actions}><button type="button" onClick={() => setSelected(item)}>查看详情</button>{item.status === 'awaiting_confirmation' || item.status === 'queue_failed' || shouldPollGeneration(item.status) ? <Link href={`/generate?generationId=${item.id}`}>继续任务</Link> : null}</div></div></article>; })}</section> : null}
    {selected ? <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="生成任务详情" onClick={() => setSelected(null)}><section className={styles.dialog} onClick={(event) => event.stopPropagation()}><button className={styles.close} type="button" onClick={() => setSelected(null)} aria-label="关闭">×</button><div className={styles.preview}>{resolveMediaUrl(selected.result_url) ? <img src={resolveMediaUrl(selected.result_url)!} alt="生成结果大图" /> : <span>{STATUS_LABELS[selected.status] || selected.status}</span>}</div><div className={styles.detail}><p>Generation detail</p><h2>{STATUS_LABELS[selected.status] || selected.status}</h2><dl><div><dt>模型</dt><dd>{selected.model}</dd></div><div><dt>费用</dt><dd>¥ {selected.cost_yuan}</dd></div><div><dt>创建时间</dt><dd>{formatTime(selected.created_at)}</dd></div><div><dt>任务 ID</dt><dd>{selected.id}</dd></div></dl>{selected.extra_prompt ? <div className={styles.prompt}><span>附加提示</span><p>{selected.extra_prompt}</p></div> : null}{selected.error_message ? <p className={styles.detailError}>{selected.error_message}</p> : null}{selected.status === 'awaiting_confirmation' || selected.status === 'queue_failed' || shouldPollGeneration(selected.status) ? <Link href={`/generate?generationId=${selected.id}`}>继续处理这个任务</Link> : null}</div></section></div> : null}
  </>;
}
