'use client';

import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import styles from './feature-page.module.css';

interface FeaturePageProps {
  kicker: string;
  title: string;
  description: string;
  cards: Array<{ title: string; description: string }>;
}

export function FeaturePage({
  kicker,
  title,
  description,
  cards,
}: FeaturePageProps) {
  return (
    <AuthGate>
      {(user) => (
        <AppShell user={user}>
          <section className={styles.heading}>
            <div>
              <p className={styles.kicker}>{kicker}</p>
              <h1>{title}</h1>
              <p className={styles.description}>{description}</p>
            </div>
            <span className={styles.badge}>基础入口已就绪</span>
          </section>

          <section className={styles.grid} aria-label="Phase 0 能力状态">
            {cards.map((card, index) => (
              <article className={styles.card} key={card.title}>
                <span className={styles.number}>0{index + 1}</span>
                <h2>{card.title}</h2>
                <p>{card.description}</p>
              </article>
            ))}
          </section>

          <aside className={styles.next}>
            <strong>当前阶段完成后</strong>
            <span>Phase 1 将接入真实照片列表、上传队列与语义检索。</span>
          </aside>
        </AppShell>
      )}
    </AuthGate>
  );
}
