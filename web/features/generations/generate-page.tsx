/* eslint-disable @next/next/no-img-element -- Photos use short-lived signed backend URLs. */
'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import { useRef, useState } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { confirmGeneration, createIdempotencyKey, getGeneration, prepareGeneration, requiresGenerationConfirmation, shouldPollGeneration, type Generation } from '@/lib/api/generations';
import { resolveMediaUrl } from '@/lib/api/media-url';
import { listPhotos } from '@/lib/api/photos';
import { getGenerationQuota, getSkill } from '@/lib/api/skills';
import { formatPhotoDate } from '@/lib/format';
import styles from './generate-page.module.css';
import { LifecyclePanel } from './lifecycle-panel';
import { FrozenInputs } from './frozen-inputs';

const STATUS_LABELS: Record<string, string> = {
  awaiting_confirmation: '等待费用确认', pending: '已入队', processing: '正在生成',
  queue_failed: '入队失败，可重试', done: '生成完成', failed: '生成失败', cancelled: '已取消', cancel_requested: '已请求停止', outcome_unknown: '结果未知', expired: '确认过期', superseded: '方案已替换',
};

export function GeneratePageView() {
  return <AuthGate>{(user) => <AppShell user={user}><GenerateWorkspace /></AppShell>}</AuthGate>;
}

export function GenerateWorkspace() {
  const params = useParams<{ skillId?: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [skillId, setSkillId] = useState<string | null>(params.skillId || null);
  const restoredGenerationId = searchParams.get('generationId');
  const [selectedPhotoId, setSelectedPhotoId] = useState<string | null>(null);
  const [extraPrompt, setExtraPrompt] = useState('');
  const [titleMode, setTitleMode] = useState<'auto' | 'none' | 'exact'>('auto');
  const [title, setTitle] = useState('');
  const [generationId, setGenerationId] = useState<string | null>(restoredGenerationId);
  const [confirmation, setConfirmation] = useState<Generation | null>(null);
  const [dismissedConfirmationId, setDismissedConfirmationId] = useState<string | null>(null);
  const idempotency = useRef<{ signature: string; key: string } | null>(null);

  const skill = useQuery({ queryKey: ['skills', 'detail', skillId], queryFn: () => getSkill(skillId!), enabled: Boolean(skillId) });
  const photos = useQuery({ queryKey: ['photos', 'generation-picker'], queryFn: () => listPhotos({ limit: 60 }) });
  const quota = useQuery({ queryKey: ['skills', 'quota'], queryFn: getGenerationQuota });
  const generation = useQuery({
    queryKey: ['generations', 'detail', generationId],
    queryFn: () => getGeneration(generationId!),
    enabled: Boolean(generationId),
    refetchInterval: (query) => shouldPollGeneration(query.state.data?.status) ? 3_000 : false,
  });

  const restoredConfirmation = generation.data
    && requiresGenerationConfirmation(generation.data.status)
    && generation.data.id !== dismissedConfirmationId
      ? generation.data
      : null;
  const effectiveConfirmation = confirmation || restoredConfirmation;

  const rememberGeneration = (item: Generation) => {
    setGenerationId(item.id);
    const base = skillId ? `/generate/${skillId}` : '/generate';
    router.replace(`${base}?generationId=${item.id}`);
  };

  const prepare = useMutation({
    mutationFn: async () => {
      if (!selectedPhotoId) throw new Error('请先选择一张源图');
      const signature = JSON.stringify([selectedPhotoId, skillId, extraPrompt.trim(), titleMode, title, skill.data?.current_version_id]);
      if (idempotency.current?.signature !== signature) idempotency.current = { signature, key: createIdempotencyKey() };
      return prepareGeneration(selectedPhotoId, {
        skill_id: skillId,
        ...(skill.data?.kind === 'package' ? { package_options: { title_mode: titleMode, title } } : {}),
        extra_prompt: extraPrompt.trim() || null,
        idempotency_key: idempotency.current.key,
      });
    },
    onSuccess: (item) => {
      rememberGeneration(item);
      setDismissedConfirmationId(null);
      if (requiresGenerationConfirmation(item.status)) setConfirmation(item);
      else setConfirmation(null);
    },
  });

  const confirm = useMutation({
    mutationFn: async () => {
      if (!effectiveConfirmation?.confirmation_token) throw new Error('确认凭证已失效，请重新创建任务');
      return confirmGeneration(effectiveConfirmation.id, effectiveConfirmation.confirmation_token, effectiveConfirmation.execution_digest);
    },
    onSuccess: async (item) => {
      setConfirmation(item.status === 'queue_failed' ? item : null);
      rememberGeneration(item);
      await queryClient.invalidateQueries({ queryKey: ['generations'] });
      await queryClient.invalidateQueries({ queryKey: ['skills', 'quota'] });
    },
  });

  const current = generation.data;
  const execution = effectiveConfirmation?.execution_snapshot;
  const adjustPlan = () => {
    if (!effectiveConfirmation) return;
    const nextSkillId = effectiveConfirmation.skill_id || skillId;
    setSkillId(nextSkillId);
    setSelectedPhotoId(effectiveConfirmation.source_photo_id);
    setExtraPrompt(effectiveConfirmation.extra_prompt || '');
    const options = effectiveConfirmation.execution_snapshot?.title_options as { title_mode?: string; title?: string } | undefined;
    if (options?.title_mode === 'auto' || options?.title_mode === 'none' || options?.title_mode === 'exact') setTitleMode(options.title_mode);
    setTitle(options?.title || '');
    setDismissedConfirmationId(effectiveConfirmation.id);
    setConfirmation(null); setGenerationId(null); idempotency.current = null;
    // Keep the current route component mounted while editing the restored draft.
    router.replace(params.skillId ? `/generate/${params.skillId}` : '/generate');
  };
  const failure = (skill.error || photos.error || quota.error || generation.error || prepare.error || confirm.error) as ApiFailure | Error | null;
  const failureText = failure?.message;

  if (!skillId && !generationId) {
    return <section className={styles.missing}><p>Start creating</p><h1>先选择一个 Skill</h1><span>生成任务需要一份创作配方；从广场挑选后会回到这里选择源图。</span><Link href="/skills">前往 Skill 广场</Link></section>;
  }

  return (
    <>
      <header className={styles.header}>
        <div><p>Generate with a skill</p><h1>{skill.data?.name || (generationId ? '生成任务' : '加载 Skill…')}</h1><span>{skill.data?.description || '选择源图并确认本次创作参数。'}</span></div>
        <div><Link href="/skills">更换 Skill</Link><Link href="/generations">生成历史</Link></div>
      </header>

      {failureText ? <div className={styles.error} role="alert">{failureText}</div> : null}

      {current ? (
        <section className={styles.progress} data-status={current.status}>
          <div className={styles.progressCopy}><span>{STATUS_LABELS[current.status] || current.status}</span><h2>{current.status === 'done' ? '新作品已经完成' : shouldPollGeneration(current.status) ? '任务正在处理中' : (STATUS_LABELS[current.status] || '请查看任务状态')}</h2><p>{current.error_message || (shouldPollGeneration(current.status) ? '你可以离开此页，稍后从生成历史继续查看。' : `任务 ID：${current.id}`)}</p><div><Link href="/generations">查看全部历史</Link>{current.status === 'failed' ? <button type="button" onClick={() => { idempotency.current = null; setGenerationId(null); router.replace(skillId ? `/generate/${skillId}` : '/generate'); }}>重新生成</button> : null}</div></div>
          <div className={styles.result}>{resolveMediaUrl(current.result_url) ? <img src={resolveMediaUrl(current.result_url)!} alt="AI 生成结果" /> : <div><span className="status-dot" /><strong>{shouldPollGeneration(current.status) ? '生成中…' : '暂无结果图'}</strong></div>}</div>
        </section>
      ) : (
        <div className={styles.layout}>
          <section>
            <div className={styles.sectionTitle}><div><span>01</span><h2>选择源图</h2></div><small>{photos.data?.length || 0} 张可选</small></div>
            {photos.isPending ? <div className={styles.loading}>正在读取相册…</div> : photos.data?.length ? <div className={styles.photoGrid}>{photos.data.map((photo) => <button type="button" key={photo.id} data-selected={selectedPhotoId === photo.id} onClick={() => setSelectedPhotoId(photo.id)}>{resolveMediaUrl(photo.thumb_url) ? <img src={resolveMediaUrl(photo.thumb_url)!} alt="" /> : <span>无缩略图</span>}<i>{selectedPhotoId === photo.id ? '✓' : formatPhotoDate(photo.taken_at)}</i></button>)}</div> : <div className={styles.empty}>相册里还没有可用照片。<Link href="/upload">先上传照片</Link></div>}
          </section>
          <aside className={styles.settings}>
            <div className={styles.sectionTitle}><div><span>02</span><h2>确认创作</h2></div></div>
            <dl><div><dt>Skill</dt><dd>{skill.data?.name || '—'}</dd></div><div><dt>模型</dt><dd>{skill.data?.kind === 'package' ? 'gpt-image-2' : skill.data?.model || '—'}</dd></div><div><dt>预计费用</dt><dd>预览时提供估算，实际费用以供应商账单为准</dd></div><div><dt>今日剩余</dt><dd>{quota.data ? `${quota.data.remaining} / ${quota.data.quota}` : '—'}</dd></div></dl>
            <label><span>附加提示（可选）</span><textarea rows={5} value={extraPrompt} onChange={(event) => setExtraPrompt(event.target.value)} placeholder="例如：保留人物表情，背景更柔和" /></label>
            {skill.data?.kind === 'package' ? <label><span>标题</span><select aria-label="标题策略" value={titleMode} onChange={(e) => setTitleMode(e.target.value as 'auto' | 'none' | 'exact')}><option value="auto">自动拟定</option><option value="none">不要文字</option><option value="exact">使用指定标题</option></select>{titleMode === 'exact' ? <input aria-label="指定标题" maxLength={60} value={title} onChange={(e) => setTitle(e.target.value)} /> : null}</label> : null}
            <button className={styles.submit} type="button" disabled={!selectedPhotoId || prepare.isPending || !skill.data} onClick={() => prepare.mutate()}>{prepare.isPending ? '正在创建任务…' : skill.data?.kind === 'package' ? '生成创作方案' : '预览费用并生成'}</button>
            <p className={styles.hint}>先预览方案与预计费用，确认后生成。修改要求会创建新方案。</p>
          </aside>
        </div>
      )}

      {current ? <LifecyclePanel key={current.id} generation={current} onChange={(item) => {
        queryClient.setQueryData(['generations', 'detail', item.id], item);
        rememberGeneration(item);
        setConfirmation(requiresGenerationConfirmation(item.status) ? item : null);
        setDismissedConfirmationId(null);
        void queryClient.invalidateQueries({ queryKey: ['generations'] });
        void queryClient.invalidateQueries({ queryKey: ['skills', 'quota'] });
      }} /> : null}

      {effectiveConfirmation ? <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="确认生成费用"><section className={styles.confirm}><button type="button" className={styles.close} onClick={() => { setConfirmation(null); setDismissedConfirmationId(effectiveConfirmation.id); }} aria-label="关闭">×</button><p>Secure confirmation</p><h2>{effectiveConfirmation.status === 'queue_failed' ? '任务入队失败，可安全重试' : '确认本次生成'}</h2>{execution ? <section className={styles.creativePlan} aria-label="创作方案"><h3>{execution.plan.concept}</h3><FrozenInputs key={effectiveConfirmation.id} generation={effectiveConfirmation} /><p>{execution.plan.observation}</p><p>保留：{execution.plan.retain.join('、')}</p><p>转化：{execution.plan.transform.join('、')}</p><p>舍弃：{execution.plan.discard.join('、')}</p><p>画幅：{execution.size} · 标题：{execution.plan.title || '无文字'}</p><p>版本：{execution.skill_version_id.slice(0, 8)}</p><ul>{execution.inputs.map((item) => <li key={item.position}>{item.role === 'subject' ? '主体源图' : '风格参考'}：{item.path}</li>)}</ul>{execution.planner_mode === 'mock' ? <p>当前为演示方案，未执行真实视觉分析。</p> : null}<details><summary>查看完整创作指令</summary><p>{String(execution.prompt || execution.plan.production_prompt)}</p></details>{effectiveConfirmation.iteration_index ? <p>这是受限调整方案；如需改写要求，请取消此任务，再从上一结果发起。</p> : <button type="button" disabled={confirm.isPending} onClick={adjustPlan}>调整方案</button>}</section> : null}<div className={styles.price}><span>预计费用</span><strong>¥ {effectiveConfirmation.estimated_cost_yuan}</strong></div><dl><div><dt>模型</dt><dd>{effectiveConfirmation.model}</dd></div><div><dt>确认有效期</dt><dd>{effectiveConfirmation.confirmation_expires_at ? new Date(effectiveConfirmation.confirmation_expires_at).toLocaleString('zh-CN') : '—'}</dd></div></dl><p className={styles.confirmHint}>最终费用与任务状态以服务端记录为准。确认操作具备幂等性。</p><div className={styles.confirmActions}><button type="button" onClick={() => { setConfirmation(null); setDismissedConfirmationId(effectiveConfirmation.id); }}>暂不生成</button><button type="button" disabled={confirm.isPending} onClick={() => confirm.mutate()}>{confirm.isPending ? '正在确认…' : effectiveConfirmation.status === 'queue_failed' ? '重试入队' : `确认生成 · 预计 ¥ ${effectiveConfirmation.estimated_cost_yuan}`}</button></div></section></div> : null}
    </>
  );
}
