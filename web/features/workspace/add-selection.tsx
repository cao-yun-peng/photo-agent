"use client";
import { useRef, useState } from 'react';
import { getWorkspace, updateWorkspace } from '@/lib/api/workspace';
import { createIdempotencyKey } from '@/lib/api/generations';
export function AddSelection({ photoId }: { photoId: string }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const flight = useRef(false);
  const add = async () => {
    if (flight.current) return;
    flight.current = true; setBusy(true);
    try {
      const current = await getWorkspace();
      await updateWorkspace({ kind: 'add_selection', photo_ids: [photoId] }, current.revision, createIdempotencyKey());
      setMessage('已加入选片区');
    } catch (error) { setMessage(error instanceof Error ? error.message : '加入失败，请重试'); }
    finally { flight.current = false; setBusy(false); }
  };
  return <span><button type="button" disabled={busy} onClick={add}>{busy ? '正在加入…' : '加入选片区'}</button>{message ? <small role="status">{message}</small> : null}</span>;
}
