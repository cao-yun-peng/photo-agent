/* eslint-disable @next/next/no-img-element -- URLs are short-lived backend/OSS signatures. */
'use client';

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import type { ApiFailure } from '@/lib/api/client';
import { resolveMediaUrl } from '@/lib/api/media-url';
import {
  deletePhoto,
  getPhoto,
  getProcessingStatuses,
  listPhotos,
  type PhotoListItem,
  reportPhotoView,
  retrySearchIndex,
} from '@/lib/api/photos';
import { formatPhotoDate } from '@/lib/format';
import styles from './photos-page.module.css';

const PAGE_SIZE = 24;
const ACTIVE_STATUSES = new Set(['indexing', 'retrying', 'service_busy']);

export function PhotosPageView() {
  return (
    <AuthGate>
      {(user) => (
        <AppShell user={user}>
          <PhotosTimeline />
        </AppShell>
      )}
    </AuthGate>
  );
}

function PhotosTimeline() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<PhotoListItem | null>(null);
  const [deleteArmed, setDeleteArmed] = useState(false);

  const timeline = useInfiniteQuery({
    queryKey: ['photos', 'timeline'],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => listPhotos({ limit: PAGE_SIZE, offset: pageParam }),
    getNextPageParam: (lastPage, pages) =>
      lastPage.length < PAGE_SIZE
        ? undefined
        : pages.reduce((total, page) => total + page.length, 0),
  });

  const photos = useMemo(
    () => timeline.data?.pages.flatMap((page) => page) || [],
    [timeline.data],
  );
  const activeIds = photos
    .filter((photo) => ACTIVE_STATUSES.has(photo.search_index_status))
    .map((photo) => photo.id);

  const statuses = useQuery({
    queryKey: ['photos', 'processing-status', activeIds],
    queryFn: () => getProcessingStatuses(activeIds),
    enabled: activeIds.length > 0,
    refetchInterval: (query) => {
      const items = query.state.data || [];
      return items.length === 0 || items.some((item) => ACTIVE_STATUSES.has(item.search_index_status))
        ? 3_000
        : false;
    },
  });
  const statusById = new Map((statuses.data || []).map((item) => [item.photo_id, item]));

  const detail = useQuery({
    queryKey: ['photos', 'detail', selected?.id],
    queryFn: () => getPhoto(selected!.id),
    enabled: Boolean(selected),
  });

  const deletion = useMutation({
    mutationFn: deletePhoto,
    onSuccess: async () => {
      setSelected(null);
      setDeleteArmed(false);
      await queryClient.invalidateQueries({ queryKey: ['photos'] });
    },
  });

  const retryIndex = useMutation({
    mutationFn: retrySearchIndex,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['photos'] });
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

  const selectPhoto = (photo: PhotoListItem) => {
    setSelected(photo);
    setDeleteArmed(false);
    reportPhotoView(photo.id, 'web_timeline').catch(() => undefined);
  };

  const failure = timeline.error as ApiFailure | null;

  return (
    <>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>Your photo memory</p>
          <h1>时间线</h1>
          <p className={styles.summary}>
            {timeline.isPending ? '正在读取照片…' : `已加载 ${photos.length} 张照片`}
          </p>
        </div>
        <div className={styles.actions}>
          <button
            className={styles.button}
            type="button"
            onClick={() => timeline.refetch()}
            disabled={timeline.isFetching}
          >
            {timeline.isFetching ? '刷新中…' : '刷新'}
          </button>
          <Link className={styles.buttonPrimary} href="/upload">
            上传照片
          </Link>
        </div>
      </header>

      {activeIds.length > 0 ? (
        <div className={styles.notice} role="status">
          <span>{activeIds.length} 张照片正在建立智能搜索索引，页面会自动更新。</span>
        </div>
      ) : null}

      {failure ? (
        <div className={styles.error} role="alert">
          <span>{failure.detail || '时间线加载失败'}</span>
          <button className={styles.button} type="button" onClick={() => timeline.refetch()}>
            重试
          </button>
        </div>
      ) : null}

      {!timeline.isPending && !failure && photos.length === 0 ? (
        <section className={styles.empty}>
          <div>
            <span className={styles.emptyMark} aria-hidden="true">＋</span>
            <h2>这里还没有照片</h2>
            <p>上传第一张照片，AI 会自动理解画面并建立中文搜索索引。</p>
            <Link className={styles.buttonPrimary} href="/upload">开始上传</Link>
          </div>
        </section>
      ) : null}

      {photos.length > 0 ? (
        <section className={styles.grid} aria-label="照片时间线">
          {photos.map((photo) => {
            const status = statusById.get(photo.id);
            const searchStatus = status?.search_index_status || photo.search_index_status;
            const imageUrl = resolveMediaUrl(photo.thumb_url);
            return (
              <button
                className={styles.photo}
                type="button"
                key={photo.id}
                onClick={() => selectPhoto(photo)}
                aria-label={`查看照片：${photo.ai_description || '正在处理'}`}
              >
                {imageUrl ? <img src={imageUrl} alt="" loading="lazy" /> : (
                  <span className={styles.placeholder}>正在生成缩略图</span>
                )}
                <span className={styles.shade} aria-hidden="true" />
                <span className={styles.meta}>
                  <span className={styles.description}>
                    {photo.ai_description || status?.message || 'AI 正在理解这张照片'}
                  </span>
                  <span className={styles.metaRow}>
                    <span>{formatPhotoDate(photo.taken_at)}</span>
                    <span className={styles.status} data-status={searchStatus}>
                      {status?.message || photo.search_index_message}
                    </span>
                  </span>
                </span>
              </button>
            );
          })}
        </section>
      ) : null}

      {timeline.hasNextPage ? (
        <div className={styles.loadMore}>
          <button
            className={styles.button}
            type="button"
            onClick={() => timeline.fetchNextPage()}
            disabled={timeline.isFetchingNextPage}
          >
            {timeline.isFetchingNextPage ? '加载中…' : '加载更多照片'}
          </button>
        </div>
      ) : null}

      {selected ? (
        <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="照片详情">
          <div className={styles.preview} onClick={() => setSelected(null)}>
            {resolveMediaUrl(selected.thumb_url) ? (
              <img
                src={resolveMediaUrl(selected.thumb_url)!}
                alt={selected.ai_description || '照片预览'}
                onClick={(event) => event.stopPropagation()}
              />
            ) : null}
          </div>
          <aside className={styles.drawer}>
            <div className={styles.drawerTop}>
              <span>Photo detail</span>
              <button className={styles.close} type="button" onClick={() => setSelected(null)} aria-label="关闭详情">×</button>
            </div>
            <h2>照片详情</h2>
            <p className={styles.drawerDescription}>
              {detail.data?.ai_description || selected.ai_description || 'AI 还在理解这张照片。'}
            </p>
            <dl className={styles.detailList}>
              <div><dt>拍摄时间</dt><dd>{formatPhotoDate(detail.data?.taken_at || selected.taken_at)}</dd></div>
              <div><dt>处理状态</dt><dd>{detail.data?.status || selected.status}</dd></div>
              <div><dt>搜索索引</dt><dd>{detail.data?.search_index_message || selected.search_index_message}</dd></div>
              <div><dt>照片 ID</dt><dd>{selected.id}</dd></div>
            </dl>
            {(detail.data?.search_index_status || selected.search_index_status) === 'unavailable' ? (
              <button
                className={styles.button}
                type="button"
                disabled={retryIndex.isPending}
                onClick={() => retryIndex.mutate(selected.id)}
              >
                {retryIndex.isPending ? '正在重试…' : '重试智能搜索索引'}
              </button>
            ) : null}
            <button
              className={styles.danger}
              type="button"
              disabled={deletion.isPending}
              onClick={() => {
                if (deleteArmed) deletion.mutate(selected.id);
                else setDeleteArmed(true);
              }}
            >
              {deletion.isPending
                ? '正在删除…'
                : deleteArmed
                  ? '再次点击，永久删除'
                  : '删除这张照片'}
            </button>
            {deletion.isError ? (
              <p className={styles.deleteError} role="alert">
                {(deletion.error as ApiFailure).detail || '删除失败，请稍后重试'}
              </p>
            ) : null}
          </aside>
        </div>
      ) : null}
    </>
  );
}
