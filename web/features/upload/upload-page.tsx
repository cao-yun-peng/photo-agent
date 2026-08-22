/* eslint-disable @next/next/no-img-element -- Local object URLs cannot use Next image optimization. */
'use client';

import { useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useRef, useState, type DragEvent } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { resolveMediaUrl } from '@/lib/api/media-url';
import { finishUpload, requestUploadUrl } from '@/lib/api/photos';
import { formatBytes } from '@/lib/format';
import { validateImageFile } from '@/lib/upload/file-policy';
import { hashFile } from '@/lib/upload/hash-file';
import { putFile } from '@/lib/upload/put-file';
import styles from './upload-page.module.css';

type UploadStatus =
  | 'queued'
  | 'hashing'
  | 'signing'
  | 'uploading'
  | 'finishing'
  | 'done'
  | 'duplicate'
  | 'failed'
  | 'cancelled';

type UploadTask = {
  id: string;
  file: File;
  mimeType: string | null;
  previewUrl: string;
  status: UploadStatus;
  progress: number;
  message: string;
  photoId?: string;
};

const STATUS_LABELS: Record<UploadStatus, string> = {
  queued: '等待上传',
  hashing: '计算文件指纹',
  signing: '申请上传地址',
  uploading: '正在直传',
  finishing: '正在登记照片',
  done: '上传完成',
  duplicate: '相册中已存在',
  failed: '上传失败',
  cancelled: '已取消',
};

const RUNNABLE_STATUSES = new Set<UploadStatus>(['queued', 'failed', 'cancelled']);
const ACTIVE_STATUSES = new Set<UploadStatus>(['hashing', 'signing', 'uploading', 'finishing']);

export function UploadPageView() {
  return (
    <AuthGate>
      {(user) => (
        <AppShell user={user}>
          <UploadWorkspace />
        </AppShell>
      )}
    </AuthGate>
  );
}

function UploadWorkspace() {
  const queryClient = useQueryClient();
  const [tasks, setTasks] = useState<UploadTask[]>([]);
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const tasksRef = useRef(tasks);
  const controllers = useRef(new Map<string, AbortController>());

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
    tasksRef.current.forEach((task) => URL.revokeObjectURL(task.previewUrl));
  }, []);

  const updateTask = (id: string, patch: Partial<UploadTask>) => {
    setTasks((current) => current.map((task) => task.id === id ? { ...task, ...patch } : task));
  };

  const addFiles = (files: FileList | File[]) => {
    const next = Array.from(files).map((file) => {
      const validation = validateImageFile(file);
      return {
        id: crypto.randomUUID(),
        file,
        mimeType: validation.mimeType,
        previewUrl: URL.createObjectURL(file),
        status: validation.error ? 'failed' : 'queued',
        progress: 0,
        message: validation.error || '已加入上传队列',
      } satisfies UploadTask;
    });
    setTasks((current) => [...current, ...next]);
  };

  const uploadOne = async (task: UploadTask) => {
    if (!task.mimeType) return;
    const controller = new AbortController();
    controllers.current.set(task.id, controller);
    try {
      updateTask(task.id, { status: 'hashing', progress: 1, message: '正在后台计算 SHA-256' });
      const hash = await hashFile(
        task.file,
        (value) => updateTask(task.id, { progress: Math.max(1, Math.round(value * 20)) }),
        controller.signal,
      );

      updateTask(task.id, { status: 'signing', progress: 20, message: '正在申请安全上传地址' });
      const signed = await requestUploadUrl({
        hash,
        size_bytes: task.file.size,
        mime_type: task.mimeType,
      });
      if (signed.duplicate) {
        updateTask(task.id, { status: 'duplicate', progress: 100, message: '相同照片已在相册中' });
        return;
      }

      const uploadUrl = resolveMediaUrl(signed.upload_url);
      if (!uploadUrl) throw new Error('服务端未返回有效上传地址');
      updateTask(task.id, { status: 'uploading', progress: 21, message: '照片正直传对象存储' });
      await putFile({
        url: uploadUrl,
        file: task.file,
        headers: signed.headers,
        signal: controller.signal,
        onProgress: (value) => updateTask(task.id, { progress: 20 + Math.round(value * 70) }),
      });

      updateTask(task.id, { status: 'finishing', progress: 92, message: '正在登记并启动 AI 处理' });
      const photo = await finishUpload({
        oss_key: signed.oss_key,
        hash,
        size_bytes: task.file.size,
        mime_type: task.mimeType,
      });
      updateTask(task.id, {
        status: 'done',
        progress: 100,
        message: '上传完成，AI 正在理解照片',
        photoId: photo.id,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        updateTask(task.id, { status: 'cancelled', message: '任务已取消，可重新上传' });
      } else {
        const failure = error as ApiFailure | Error;
        updateTask(task.id, {
          status: 'failed',
          message: 'detail' in failure ? failure.detail : failure.message || '上传失败，请重试',
        });
      }
    } finally {
      controllers.current.delete(task.id);
    }
  };

  const startQueue = async () => {
    const queue = tasksRef.current.filter((task) => RUNNABLE_STATUSES.has(task.status) && task.mimeType);
    if (queue.length === 0 || running) return;
    setRunning(true);
    let cursor = 0;
    const worker = async () => {
      while (cursor < queue.length) {
        const task = queue[cursor];
        cursor += 1;
        await uploadOne(task);
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
    await queryClient.invalidateQueries({ queryKey: ['photos'] });
    setRunning(false);
  };

  const removeTask = (task: UploadTask) => {
    controllers.current.get(task.id)?.abort();
    URL.revokeObjectURL(task.previewUrl);
    setTasks((current) => current.filter((item) => item.id !== task.id));
  };

  const runnableCount = tasks.filter((task) => RUNNABLE_STATUSES.has(task.status) && task.mimeType).length;
  const completeCount = tasks.filter((task) => task.status === 'done' || task.status === 'duplicate').length;

  return (
    <>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>Direct to object storage</p>
          <h1>上传照片</h1>
          <p className={styles.summary}>文件不经过应用服务器，最多同时上传 3 张。</p>
        </div>
        {completeCount > 0 ? <Link className={styles.secondary} href="/photos">查看时间线</Link> : null}
      </header>

      <label
        className={styles.dropzone}
        data-dragging={dragging}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={(event: DragEvent<HTMLLabelElement>) => {
          event.preventDefault();
          setDragging(false);
          addFiles(event.dataTransfer.files);
        }}
      >
        <input
          type="file"
          multiple
          accept="image/jpeg,image/png,image/webp,image/heic,image/heif,image/gif,.heic,.heif"
          onChange={(event) => {
            if (event.target.files) addFiles(event.target.files);
            event.target.value = '';
          }}
        />
        <span className={styles.dropIcon} aria-hidden="true">＋</span>
        <strong>拖拽照片到这里，或点击选择</strong>
        <span>支持 JPG、PNG、WebP、HEIC、HEIF、GIF · 单张最大 100 MB</span>
      </label>

      {tasks.length > 0 ? (
        <section className={styles.queue} aria-label="上传队列">
          <div className={styles.queueHeader}>
            <div>
              <h2>上传队列</h2>
              <p>{tasks.length} 个任务 · {completeCount} 个已完成</p>
            </div>
            <button
              className={styles.primary}
              type="button"
              onClick={startQueue}
              disabled={running || runnableCount === 0}
            >
              {running ? '队列上传中…' : `开始上传${runnableCount ? `（${runnableCount}）` : ''}`}
            </button>
          </div>

          <div className={styles.taskList}>
            {tasks.map((task) => {
              const active = ACTIVE_STATUSES.has(task.status);
              return (
                <article className={styles.task} key={task.id}>
                  <img src={task.previewUrl} alt="" />
                  <div className={styles.taskBody}>
                    <div className={styles.taskTop}>
                      <div>
                        <h3 title={task.file.name}>{task.file.name}</h3>
                        <p>{formatBytes(task.file.size)} · {STATUS_LABELS[task.status]}</p>
                      </div>
                      <span className={styles.percent}>{task.progress}%</span>
                    </div>
                    <div className={styles.progress} aria-label={`上传进度 ${task.progress}%`}>
                      <span style={{ width: `${task.progress}%` }} />
                    </div>
                    <div className={styles.taskBottom}>
                      <span data-error={task.status === 'failed'}>{task.message}</span>
                      <div>
                        {active ? (
                          <button type="button" onClick={() => controllers.current.get(task.id)?.abort()}>取消</button>
                        ) : null}
                        <button type="button" onClick={() => removeTask(task)}>移除</button>
                      </div>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}
    </>
  );
}
