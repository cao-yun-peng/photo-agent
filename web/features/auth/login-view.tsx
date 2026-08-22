'use client';

import { useMutation } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { FormEvent, useEffect, useState } from 'react';
import { loginWithDevelopmentUser } from '@/lib/api/auth';
import { API_ORIGIN, type ApiFailure } from '@/lib/api/client';
import { readSession, saveSession } from '@/lib/auth/session';
import styles from './login-view.module.css';

const devLoginEnabled = process.env.NEXT_PUBLIC_ENABLE_DEV_LOGIN !== 'false';

export function LoginView() {
  const router = useRouter();
  const [nickname, setNickname] = useState('Web Developer');

  useEffect(() => {
    if (readSession()) router.replace('/photos');
  }, [router]);

  const loginMutation = useMutation({
    mutationFn: () =>
      loginWithDevelopmentUser({
        code: `web-dev-${nickname.trim() || 'developer'}`,
        nickname: nickname.trim() || 'Web Developer',
      }),
    onSuccess: (token) => {
      saveSession(token.access_token, token.expires_in);
      router.push('/photos');
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (devLoginEnabled && !loginMutation.isPending) loginMutation.mutate();
  };

  const failure = loginMutation.error as ApiFailure | null;

  return (
    <main className={styles.page}>
      <section className={styles.story} aria-labelledby="login-heading">
        <div className={styles.brand}>
          <span className="brand-mark" aria-hidden="true" />
          <span>
            <strong>Photo Agent</strong>
            <span>Web workspace</span>
          </span>
        </div>

        <div className={styles.hero}>
          <p className={styles.eyebrow}>照片，从存下到重新发现</p>
          <h1 id="login-heading">让相册真正听懂你。</h1>
          <p>
            在浏览器里完成照片入库、中文语义检索和 AI 二次创作，获得更直接的开发、联调与验收体验。
          </p>
        </div>

        <div className={styles.capabilities} aria-label="核心能力">
          <span>智能时间线</span>
          <span>自然语言搜索</span>
          <span>Photo Agent</span>
          <span>Skill 二创</span>
        </div>
      </section>

      <section className={styles.panelWrap} aria-label="开发态登录">
        <form className={styles.panel} onSubmit={submit}>
          <span className={styles.mode}>
            <span className="status-dot" aria-hidden="true" />
            Development mode
          </span>
          <h2>进入工作台</h2>
          <p className={styles.intro}>
            使用后端的开发态 Mock 微信登录创建隔离用户，无需真实微信凭据。
          </p>

          <div className={styles.field}>
            <label htmlFor="nickname">开发用户昵称</label>
            <input
              id="nickname"
              name="nickname"
              value={nickname}
              maxLength={64}
              autoComplete="name"
              onChange={(event) => setNickname(event.target.value)}
            />
          </div>

          <button
            className={styles.submit}
            type="submit"
            disabled={!devLoginEnabled || loginMutation.isPending}
          >
            {loginMutation.isPending ? '正在连接…' : '使用开发用户进入'}
          </button>

          {failure ? (
            <p className={styles.error} role="alert">
              {failure.detail || failure.message}
              {failure.logId ? ` · Log ID: ${failure.logId}` : ''}
            </p>
          ) : null}

          <p className={styles.endpoint}>API · {API_ORIGIN}</p>
          <p className={styles.notice}>
            此入口仅在本地开发环境启用；正式 Web 登录将在生产发布阶段单独接入。
          </p>
        </form>
      </section>
    </main>
  );
}
