"use client";
import { useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { getGenerationCost, cancelGeneration, iterateGeneration, createIdempotencyKey, type Generation } from '@/lib/api/generations';

const STAGES: Record<string, string> = { awaiting_confirmation: '等待方案确认', queued: '等待 Worker', validating: '核对冻结输入', generating: '模型生成中', verifying: '核验结果', storing: '保存结果', done: '已完成', cancelled: '已取消', cancel_requested: '等待停止结果', outcome_unknown: '供应商结果未知', expired: '方案已过期', failed: '任务失败', queue_failed: '等待重新入队' };
export function LifecyclePanel({ generation, onChange }: { generation: Generation; onChange: (item: Generation) => void }) {
  const cost = useQuery({ queryKey: ['generation-cost', generation.id, generation.status], queryFn: () => getGenerationCost(generation.id), enabled: Boolean(generation.execution_snapshot), retry: false, refetchInterval: ['pending', 'processing'].includes(generation.status) ? 5000 : false });
  const [feedback, setFeedback] = useState('');
  const key = useRef<{ feedback: string; key: string } | null>(null);
  const cancel = useMutation({ mutationFn: () => cancelGeneration(generation.id), onSuccess: onChange });
  const revise = useMutation({ mutationFn: () => {
    if (key.current?.feedback !== feedback.trim()) key.current = { feedback: feedback.trim(), key: createIdempotencyKey() };
    return iterateGeneration(generation.id, feedback.trim(), key.current.key);
  }, onSuccess: onChange });
  if (!generation.execution_snapshot) return null;
  const verification = generation.verification as Record<string, unknown> | null | undefined;
  return <section aria-label="生成任务控制">
    <p role="status">{generation.status === 'cancel_requested' ? STAGES.cancel_requested : STAGES[generation.progress_stage] || STAGES[generation.status] || generation.status} · 第 {generation.iteration_index || 0} 次调整</p>
    {generation.status === 'outcome_unknown' ? <p>供应商可能仍在处理或已计费。请先核实结果；系统不会自动重画。</p> : null}
    {cost.data ? <p>已记录 {cost.data.call_count} 次调用，已知费用估算 ¥{cost.data.estimated_yuan_known.toFixed(4)}{cost.data.unknown_estimate_count ? `；另有 ${cost.data.unknown_estimate_count} 次费用未知` : ''}。尚未与账单核对；不含父任务费用。</p> : null}
    {cost.error ? <p>费用记录暂时不可用，不能据此认定未计费。</p> : null}
    {verification?.simulated ? <p>这是演示结果，未调用真实生图模型。</p> : null}
    {verification?.status === 'needs_review' ? <p>结果仍需人工检查，系统不会自动重画。</p> : null}
    {Array.isArray(verification?.issues) ? <ul>{verification.issues.filter((issue): issue is string => typeof issue === 'string').map((issue, index) => <li key={index}>{issue}</li>)}</ul> : null}
    {['awaiting_confirmation', 'pending', 'queue_failed', 'processing'].includes(generation.status) ? <><button type="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>{cancel.isPending ? '正在请求取消…' : '取消本次任务'}</button><p>模型已开始时可能无法撤销供应商处理或费用。</p></> : null}
    {generation.status === 'done' ? <div><label>需要调整的内容<textarea aria-label="结果调整要求" maxLength={500} value={feedback} onChange={(event) => setFeedback(event.target.value)} /></label><p>将沿用冻结原图和风格参考创建新方案，每轮需要重新确认，次数与累计费用估算由服务端限制。</p><button type="button" disabled={!feedback.trim() || revise.isPending} onClick={() => revise.mutate()}>{revise.isPending ? '正在准备调整方案…' : '预览调整方案'}</button></div> : null}
    {cancel.error || revise.error ? <p role="alert">{(cancel.error || revise.error)?.message}</p> : null}
  </section>;
}
