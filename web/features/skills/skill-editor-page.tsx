/* eslint-disable @next/next/no-img-element -- Local previews and signed backend URLs cannot use next/image. */
'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { resolveMediaUrl } from '@/lib/api/media-url';
import { requestUploadUrl } from '@/lib/api/photos';
import { createSkill, getSkill, updateSkill, type SkillCreate } from '@/lib/api/skills';
import { validateImageFile } from '@/lib/upload/file-policy';
import { hashFile } from '@/lib/upload/hash-file';
import { putFile } from '@/lib/upload/put-file';
import styles from './skill-editor-page.module.css';

type Reference = { key: string; url: string | null; local: boolean };

const INITIAL: SkillCreate = {
  name: '', description: '', prompt_template: '', reference_keys: [], cover_key: null,
  model: 'wanx2.1-imageedit', function: 'description_edit', strength: 0.7, is_public: false,
};

export function SkillEditorPage({ mode }: { mode: 'create' | 'edit' }) {
  return (
    <AuthGate>
      {(user) => <AppShell user={user}><SkillEditor mode={mode} /></AppShell>}
    </AuthGate>
  );
}

function SkillEditor({ mode }: { mode: 'create' | 'edit' }) {
  const params = useParams<{ id: string }>();
  const skillId = mode === 'edit' ? params.id : null;
  const router = useRouter();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<SkillCreate>(INITIAL);
  const [references, setReferences] = useState<Reference[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [formError, setFormError] = useState<string | null>(null);
  const initialized = useRef(false);
  const referencesRef = useRef(references);

  useEffect(() => {
    referencesRef.current = references;
  }, [references]);

  const detail = useQuery({
    queryKey: ['skills', 'detail', skillId],
    queryFn: () => getSkill(skillId!),
    enabled: Boolean(skillId),
  });

  useEffect(() => {
    const skill = detail.data;
    if (!skill || initialized.current) return;
    initialized.current = true;
    setForm({
      name: skill.name,
      description: skill.description || '',
      prompt_template: skill.prompt_template,
      reference_keys: skill.reference_keys,
      cover_key: skill.cover_key || null,
      model: skill.model,
      function: skill.function,
      strength: skill.strength,
      is_public: skill.is_public,
    });
    setReferences(skill.reference_keys.map((key) => ({
      key,
      url: key === skill.cover_key ? resolveMediaUrl(skill.cover_url) : null,
      local: false,
    })));
  }, [detail.data]);

  useEffect(() => () => {
    referencesRef.current.filter((item) => item.local && item.url).forEach((item) => URL.revokeObjectURL(item.url!));
  }, []);

  const save = useMutation({
    mutationFn: async () => {
      const payload = { ...form, reference_keys: references.map((item) => item.key) };
      if (!payload.name.trim()) throw new Error('请填写 Skill 名称');
      if (!payload.prompt_template.trim()) throw new Error('请填写提示词模板');
      return skillId ? updateSkill(skillId, payload) : createSkill(payload);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['skills'] });
      router.push('/skills?tab=mine');
    },
  });

  const uploadReference = async (file: File) => {
    setFormError(null);
    if (references.length >= 5) return setFormError('参考图最多上传 5 张');
    const validation = validateImageFile(file);
    if (validation.error || !validation.mimeType) return setFormError(validation.error || '图片格式不支持');
    setUploading(true);
    setUploadProgress(0);
    try {
      const hash = await hashFile(file, (progress) => setUploadProgress(progress * 0.35));
      const signed = await requestUploadUrl({ hash, size_bytes: file.size, mime_type: validation.mimeType });
      if (signed.duplicate || !signed.oss_key) throw new Error('这张参考图已存在相册中，请换一张');
      const uploadUrl = resolveMediaUrl(signed.upload_url);
      if (!uploadUrl) throw new Error('上传地址无效');
      await putFile({
        url: uploadUrl, file, headers: signed.headers,
        onProgress: (progress) => setUploadProgress(0.35 + progress * 0.65),
      });
      const preview = URL.createObjectURL(file);
      setReferences((items) => [...items, { key: signed.oss_key, url: preview, local: true }]);
      setForm((value) => ({ ...value, cover_key: value.cover_key || signed.oss_key }));
      setUploadProgress(1);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '参考图上传失败');
    } finally {
      setUploading(false);
    }
  };

  const removeReference = (item: Reference) => {
    if (item.local && item.url) URL.revokeObjectURL(item.url);
    const remaining = references.filter((reference) => reference.key !== item.key);
    setReferences(remaining);
    setForm((value) => ({
      ...value,
      cover_key: value.cover_key === item.key ? remaining[0]?.key || null : value.cover_key,
    }));
  };

  const failure = (detail.error || save.error) as ApiFailure | Error | null;
  const errorText = formError || failure?.message;

  if (detail.isPending && skillId) return <div className={styles.loading}>正在载入 Skill…</div>;

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <div><p>Skill workshop</p><h1>{mode === 'create' ? '新建 Skill' : '编辑 Skill'}</h1><span>把一次好用的创作方法保存成可以反复使用的配方。</span></div>
        <Link href="/skills?tab=mine">返回我的 Skill</Link>
      </header>

      <form className={styles.form} onSubmit={(event) => { event.preventDefault(); save.mutate(); }}>
        <section className={styles.panel}>
          <h2>基础信息</h2>
          <label><span>名称 *</span><input maxLength={50} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：复古胶片人像" /></label>
          <label><span>简介</span><textarea maxLength={300} rows={3} value={form.description || ''} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="告诉使用者这个 Skill 能做什么" /></label>
          <label><span>提示词模板 *</span><textarea rows={8} value={form.prompt_template} onChange={(event) => setForm({ ...form, prompt_template: event.target.value })} placeholder="描述希望模型如何编辑照片…" /></label>
          <div className={styles.columns}>
            <label><span>模型</span><select value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })}><option value="wanx2.1-imageedit">Wanx Image Edit</option><option value="gpt-image-2">GPT Image 2</option></select></label>
            <label><span>编辑方式</span><select value={form.function} onChange={(event) => setForm({ ...form, function: event.target.value })}><option value="description_edit">描述式编辑</option><option value="stylization_all">整体风格化</option><option value="stylization_local">局部风格化</option></select></label>
          </div>
          <label><span>变化强度 <strong>{Math.round(form.strength * 100)}%</strong></span><input type="range" min="0" max="1" step="0.05" value={form.strength} onChange={(event) => setForm({ ...form, strength: Number(event.target.value) })} /></label>
          <label className={styles.toggle}><input type="checkbox" checked={form.is_public} onChange={(event) => setForm({ ...form, is_public: event.target.checked })} /><span><strong>发布到 Skill 广场</strong><small>其他用户可以看到并使用，但不能修改。</small></span></label>
        </section>

        <aside className={styles.panel}>
          <div className={styles.panelTitle}><div><h2>参考图</h2><p>最多 5 张，第一张作为封面。</p></div><span>{references.length}/5</span></div>
          <label className={styles.drop}><input type="file" accept="image/*" disabled={uploading || references.length >= 5} onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadReference(file); event.currentTarget.value = ''; }} /><strong>{uploading ? `上传中 ${Math.round(uploadProgress * 100)}%` : '＋ 添加参考图'}</strong><span>JPG、PNG、WebP、HEIC 或 GIF</span></label>
          <div className={styles.refs}>{references.map((item, index) => <div className={styles.ref} key={item.key}>{item.url ? <img src={item.url} alt={`参考图 ${index + 1}`} /> : <span>REF {index + 1}</span>}<button type="button" onClick={() => removeReference(item)} aria-label="移除参考图">×</button>{form.cover_key === item.key ? <em>封面</em> : <button className={styles.coverButton} type="button" onClick={() => setForm({ ...form, cover_key: item.key })}>设为封面</button>}</div>)}</div>
          <div className={styles.preview}><span>效果预览</span><h3>{form.name || '你的 Skill 名称'}</h3><p>{form.description || '简介会显示在 Skill 广场与详情中。'}</p><small>{form.model} · {Math.round(form.strength * 100)}%</small></div>
        </aside>

        {errorText ? <p className={styles.error} role="alert">{errorText}</p> : null}
        <div className={styles.actions}><Link href="/skills?tab=mine">取消</Link><button type="submit" disabled={save.isPending || uploading}>{save.isPending ? '正在保存…' : mode === 'create' ? '创建 Skill' : '保存修改'}</button></div>
      </form>
    </div>
  );
}
