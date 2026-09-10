/* eslint-disable @next/next/no-img-element -- Private input previews use authenticated blobs. */
'use client';
import { useEffect, useState } from 'react';
import { getGenerationInput, type Generation } from '@/lib/api/generations';
import styles from './generate-page.module.css';

export function FrozenInputs({ generation }: { generation: Generation }) {
  const [images, setImages] = useState<string[]>([]);
  const [error, setError] = useState('');
  const inputs = generation.execution_snapshot?.inputs;
  useEffect(() => {
    let active = true;
    const urls: string[] = [];
    if (!inputs) return;
    Promise.all(inputs.map((item) => getGenerationInput(generation.id, item.position))).then((blobs) => {
      if (!active) return;
      urls.push(...blobs.map((blob) => URL.createObjectURL(blob)));
      setImages([...urls]);
    }).catch(() => { if (active) setError('参考图预览暂不可用，请稍后重试。'); });
    return () => { active = false; urls.forEach((url) => URL.revokeObjectURL(url)); };
  }, [generation.id, inputs]);
  return <div className={styles.frozenInputs}>{inputs?.map((item, i) => <figure key={item.position}>
    {images[i] ? <img src={images[i]} alt={item.role === 'subject' ? '冻结主体源图' : '冻结风格参考图'} /> : null}
    <figcaption>{item.role === 'subject' ? '主体源图' : '风格参考'}</figcaption>
  </figure>)}{error ? <p role="status">{error}</p> : null}</div>;
}
