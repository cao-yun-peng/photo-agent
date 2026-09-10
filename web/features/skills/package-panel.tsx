/* eslint-disable @next/next/no-img-element -- Package previews are validated raster data or private blobs. */
'use client';

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { importSkillPackage, listSkillVersions, previewSkillPackage, readSkillAsset, type PackageReport } from '@/lib/api/skills';
import styles from './package-panel.module.css';

function Report({ report }: { report: PackageReport }) {
  return <div className={styles.report}>
    <h3>{report.name}</h3><p>{report.description}</p>
    {report.cover_data_url ? <img src={report.cover_data_url} alt="包内参考图" /> : null}
    <p>包含 {report.assets.length} 个文件 · {report.assets.reduce((sum, a) => sum + a.size, 0).toLocaleString()} 字节</p>
    <p>支持：{report.supported?.join('、')}</p>
    <p>许可证：{report.license || '未声明'}{report.source ? ` · 来源：${report.source}` : ''}</p>
    <p>可先预览创作方案，确认后生成。</p><ul>{report.warnings?.filter((text) => !text.includes('执行尚未接通')).map((text) => <li key={text}>{text}</li>)}</ul>
    {report.errors?.length ? <ul role="alert">{report.errors.map((text) => <li key={text}>{text}</li>)}</ul> : null}
  </div>;
}

export function PackageUpload({ skillId, onSaved }: { skillId?: string; onSaved: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<PackageReport | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  async function preview(next: File | null) {
    const epoch = ++generation.current;
    setFile(next); setReport(null); setError(''); setMessage('');
    if (!next) return;
    if (next.size > 16 * 1024 * 1024) { setError('ZIP不得超过16 MiB'); return; }
    setPending(true);
    try {
      const result = await previewSkillPackage(next);
      if (epoch === generation.current) setReport(result);
    } catch (e) { if (epoch === generation.current) setError(e instanceof Error ? e.message : '预览失败'); }
    finally { if (epoch === generation.current) setPending(false); }
  }
  async function save() {
    if (!file || !report?.can_import) return;
    const epoch = generation.current;
    setPending(true); setError('');
    try {
      const result = await importSkillPackage(file, report.content_sha256, skillId);
      if (epoch !== generation.current) return;
      setMessage(result.deduplicated ? '相同内容已保存，未新增或切换版本。' : '已保存为私有流程包，可查看资源和版本。');
      setReport(null); setFile(null); onSaved();
    } catch (e) { if (epoch === generation.current) setError(e instanceof Error ? e.message : '导入失败'); }
    finally { if (epoch === generation.current) setPending(false); }
  }
  return <section className={styles.panel} aria-label="导入Skill流程包">
    <h2>{skillId ? '上传新版本' : '导入 Skill 流程包'}</h2>
    <p>选择包含 SKILL.md 的 ZIP，先查看资源与兼容报告，再保存到我的 Skill。最大16 MiB。</p>
    <label>选择 ZIP<input aria-label="选择Skill ZIP" type="file" accept=".zip,application/zip" disabled={pending} onChange={(e) => void preview(e.target.files?.[0] || null)} /></label>
    {pending ? <p role="status">正在处理…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {report ? <><Report report={report} /><button type="button" disabled={pending || !report.can_import} onClick={() => void save()}>保存私有{skillId ? '新版本' : '流程包'}</button></> : null}
    {message ? <p role="status">{message}</p> : null}
  </section>;
}

export function PackageVersions({ skillId }: { skillId: string }) {
  const versions = useQuery({ queryKey: ['skills', skillId, 'versions'], queryFn: () => listSkillVersions(skillId) });
  const [asset, setAsset] = useState<{ url?: string; text?: string }>({});
  const [error, setError] = useState('');
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  useEffect(() => () => { if (asset.url) URL.revokeObjectURL(asset.url); }, [asset]);
  async function open(version: string, path: string) {
    const epoch = ++generation.current;
    setError(''); setAsset({});
    try {
      const blob = await readSkillAsset(skillId, version, path);
      const content = blob.type.startsWith('image/') ? { url: URL.createObjectURL(blob) } : { text: await blob.text() };
      if (epoch === generation.current) setAsset(content);
      else if (content.url) URL.revokeObjectURL(content.url);
    } catch (e) { if (epoch === generation.current) setError(e instanceof Error ? e.message : '读取资源失败'); }
  }
  return <section className={styles.panel} aria-label="流程包版本">
    <h3>版本与资源</h3>
    {versions.isPending ? <p>正在读取版本…</p> : null}
    {versions.error || error ? <p role="alert">{error || versions.error?.message}</p> : null}
    {versions.data?.map((v, i) => <details key={v.id} open={i === 0}>
      <summary>{v.report.name} · {v.report.content_sha256.slice(0, 12)}</summary>
      <Report report={v.report} />
      <details><summary>入口说明</summary><pre>{v.instructions}</pre></details>
      <ul>{v.report.assets.map((a) => <li key={a.path}><button type="button" onClick={() => void open(v.id, a.path)}>{a.path}</button></li>)}</ul>
    </details>)}
    {asset.url ? <img className={styles.asset} src={asset.url} alt="所选包内图片资源" /> : null}
    {asset.text ? <pre>{asset.text}</pre> : null}
  </section>;
}
