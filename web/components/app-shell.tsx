'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import type { CurrentUser } from '@/lib/api/auth';
import { clearSession } from '@/lib/auth/session';
import styles from './app-shell.module.css';

const navItems = [
  { href: '/photos', label: '时间线', glyph: '片' },
  { href: '/upload', label: '上传', glyph: '传' },
  { href: '/search', label: '智能搜索', glyph: '搜' },
  { href: '/skills', label: 'Skill 广场', glyph: '技' },
  { href: '/generations', label: '生成历史', glyph: '创' },
];

export function AppShell({
  user,
  children,
}: {
  user: CurrentUser;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();

  const logout = () => {
    clearSession();
    router.replace('/login');
  };

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <Link className={styles.brand} href="/photos" aria-label="Photo Agent 首页">
          <span className="brand-mark" aria-hidden="true" />
          <span>
            <strong>Photo Agent</strong>
            <small>Web workspace</small>
          </span>
        </Link>

        <nav className={styles.nav} aria-label="主要导航">
          {navItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              data-active={pathname === item.href}
            >
              <span className={styles.navGlyph} aria-hidden="true">
                {item.glyph}
              </span>
              {item.label}
            </Link>
          ))}
        </nav>

        <div className={styles.account}>
          <p className={styles.accountName}>{user.nickname || '开发用户'}</p>
          <p className={styles.accountMeta}>开发态会话 · 当前标签页</p>
          <button className={styles.logout} type="button" onClick={logout}>
            退出开发会话
          </button>
        </div>
      </aside>

      <main className={styles.main}>
        <header className={styles.topbar}>
          <span className={styles.phase}>Phase 2 · Agent search</span>
          <span className={styles.environment}>
            <span className="status-dot" aria-hidden="true" />
            FastAPI 已连接
          </span>
        </header>
        <div className={styles.content}>{children}</div>
      </main>
    </div>
  );
}
