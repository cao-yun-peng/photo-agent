/* eslint-disable @next/next/no-img-element -- Skill covers use signed backend URLs. */
'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { resolveMediaUrl } from '@/lib/api/media-url';
import {
  deleteSkill,
  getGenerationQuota,
  listSkills,
  type Skill,
} from '@/lib/api/skills';
import styles from './skills-page.module.css';

const FUNCTION_LABELS: Record<string, string> = {
  description_edit: '描述式编辑',
  stylization_all: '整体风格化',
  stylization_local: '局部风格化',
};

export function SkillsPageView() {
  return (
    <AuthGate>
      {(user) => (
        <AppShell user={user}>
          <SkillsWorkspace userId={user.id} />
        </AppShell>
      )}
    </AuthGate>
  );
}

export function SkillsWorkspace({ userId }: { userId: string }) {
  const searchParams = useSearchParams();
  const [scope, setScope] = useState<'plaza' | 'mine'>(
    searchParams.get('tab') === 'mine' ? 'mine' : 'plaza',
  );
  const [selected, setSelected] = useState<Skill | null>(null);
  const [deleteArmed, setDeleteArmed] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const skills = useQuery({
    queryKey: ['skills', scope],
    queryFn: () => listSkills(scope),
  });
  const quota = useQuery({
    queryKey: ['skills', 'quota'],
    queryFn: getGenerationQuota,
  });
  const deletion = useMutation({
    mutationFn: deleteSkill,
    onSuccess: async () => {
      setSelected(null);
      setDeleteArmed(null);
      await queryClient.invalidateQueries({ queryKey: ['skills'] });
    },
  });

  useEffect(() => {
    if (!selected) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelected(null);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [selected]);

  const failure = (skills.error || quota.error || deletion.error) as ApiFailure | null;
  const items = skills.data || [];

  return (
    <>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>Creative recipes</p>
          <h1>Skill 广场</h1>
          <p>选择一种创作配方，再从你的相册里挑选源图开始生成。</p>
        </div>
        <div className={styles.headerActions}>
          <Link href="/generations">生成历史</Link>
          <Link className={styles.primary} href="/skills/new">＋ 新建 Skill</Link>
        </div>
      </header>

      <section className={styles.toolbar}>
        <div className={styles.tabs} role="tablist" aria-label="Skill 范围">
          <button type="button" role="tab" aria-selected={scope === 'plaza'} onClick={() => setScope('plaza')}>
            广场
          </button>
          <button type="button" role="tab" aria-selected={scope === 'mine'} onClick={() => setScope('mine')}>
            我的 Skill
          </button>
        </div>
        <div className={styles.quota}>
          <span>今日剩余</span>
          <strong>{quota.data ? quota.data.remaining : '—'}</strong>
          <small>/ {quota.data ? quota.data.quota : '—'} 次</small>
        </div>
      </section>

      {failure ? (
        <div className={styles.error} role="alert">
          <span>{failure.detail || '加载失败，请稍后重试'}</span>
          <button type="button" onClick={() => void skills.refetch()}>重试</button>
        </div>
      ) : null}

      {skills.isPending ? (
        <div className={styles.loading}><span className="status-dot" />正在整理 Skill…</div>
      ) : items.length ? (
        <section className={styles.grid} aria-label={scope === 'plaza' ? 'Skill 广场' : '我的 Skill'}>
          {items.map((skill, index) => {
            const cover = resolveMediaUrl(skill.cover_url);
            const own = !skill.is_official && skill.owner_id === userId;
            return (
              <article className={styles.card} key={skill.id}>
                <button className={styles.cover} type="button" onClick={() => setSelected(skill)}>
                  {cover ? <img src={cover} alt="" loading="lazy" /> : (
                    <span data-variant={index % 3}><i>{skill.name.slice(0, 1)}</i></span>
                  )}
                  <em>{skill.is_official ? '官方' : skill.is_public ? '公开' : '私有'}</em>
                </button>
                <div className={styles.cardBody}>
                  <div>
                    <h2>{skill.name}</h2>
                    <p>{skill.description || '用这份提示词为照片创造新的表达。'}</p>
                  </div>
                  <div className={styles.meta}>
                    <span>{FUNCTION_LABELS[skill.function] || skill.function}</span>
                    <span>使用 {skill.use_count} 次</span>
                  </div>
                  <div className={styles.cardActions}>
                    <button type="button" onClick={() => setSelected(skill)}>查看</button>
                    {own ? <Link href={`/skills/${skill.id}/edit`}>编辑</Link> : null}
                    <Link className={styles.use} href={`/generate/${skill.id}`}>使用这个 Skill</Link>
                  </div>
                </div>
              </article>
            );
          })}
        </section>
      ) : (
        <section className={styles.empty}>
          <span aria-hidden="true">＋</span>
          <h2>{scope === 'mine' ? '还没有自定义 Skill' : '广场暂时是空的'}</h2>
          <p>写下你的创作想法，把它保存成可重复使用的照片配方。</p>
          <Link href="/skills/new">创建第一个 Skill</Link>
        </section>
      )}

      {selected ? (
        <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="Skill 详情" onClick={() => setSelected(null)}>
          <section className={styles.dialog} onClick={(event) => event.stopPropagation()}>
            <button className={styles.close} type="button" onClick={() => setSelected(null)} aria-label="关闭">×</button>
            <p className={styles.dialogKicker}>{selected.is_official ? 'Official Skill' : 'Custom Skill'}</p>
            <h2>{selected.name}</h2>
            <p className={styles.dialogDescription}>{selected.description || '暂无简介'}</p>
            <div className={styles.prompt}>
              <span>提示词</span>
              <p>{selected.prompt_template}</p>
            </div>
            <dl>
              <div><dt>模型</dt><dd>{selected.model}</dd></div>
              <div><dt>编辑方式</dt><dd>{FUNCTION_LABELS[selected.function] || selected.function}</dd></div>
              <div><dt>强度</dt><dd>{Math.round(selected.strength * 100)}%</dd></div>
              <div><dt>参考图</dt><dd>{selected.reference_keys.length} 张</dd></div>
            </dl>
            <div className={styles.dialogActions}>
              {!selected.is_official && selected.owner_id === userId ? (
                <>
                  <Link href={`/skills/${selected.id}/edit`}>编辑 Skill</Link>
                  <button
                    type="button"
                    data-danger={deleteArmed === selected.id}
                    disabled={deletion.isPending}
                    onClick={() => {
                      if (deleteArmed === selected.id) deletion.mutate(selected.id);
                      else setDeleteArmed(selected.id);
                    }}
                  >
                    {deleteArmed === selected.id ? '再次点击确认删除' : '删除'}
                  </button>
                </>
              ) : null}
              <Link className={styles.dialogPrimary} href={`/generate/${selected.id}`}>选择源图并生成</Link>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}
