'use client';

import { useMutation, useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { FormEvent, useEffect, useState } from 'react';
import { getAuthOptions, loginWithDevelopmentUser, loginWithPassword, registerWithPassword } from '@/lib/api/auth';
import type { ApiFailure } from '@/lib/api/client';
import { readSession, saveSession } from '@/lib/auth/session';
import styles from './login-view.module.css';

export function LoginView() {
  const router = useRouter();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [nickname, setNickname] = useState('Web Developer');
  const [formError, setFormError] = useState('');
  const options = useQuery({ queryKey: ['auth-options'], queryFn: getAuthOptions, retry: false });
  const devLoginEnabled = process.env.NEXT_PUBLIC_ENABLE_DEV_LOGIN === 'true'
    && options.data?.development_login_enabled === true;

  useEffect(() => {
    if (readSession()) router.replace('/photos');
  }, [router]);

  const mutation = useMutation({
    mutationFn: async (development: boolean) => {
      if (development) {
        return loginWithDevelopmentUser({ code: `web-dev-${nickname.trim() || 'developer'}`, nickname });
      }
      const payload = { username: username.trim().toLowerCase(), password };
      return mode === 'register' ? registerWithPassword(payload) : loginWithPassword(payload);
    },
    onSuccess: (token) => {
      setPassword('');
      setConfirmation('');
      saveSession(token.access_token, token.expires_in);
      router.push('/photos');
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (mutation.isPending) return;
    setFormError('');
    if (mode === 'register' && password !== confirmation) {
      setFormError('两次输入的密码不一致');
      return;
    }
    if (mode === 'register' && !options.data?.registration_enabled) return;
    mutation.mutate(false);
  };

  const switchMode = () => {
    setMode(mode === 'login' ? 'register' : 'login');
    setPassword('');
    setConfirmation('');
    setFormError('');
    mutation.reset();
  };
  const failure = mutation.error as ApiFailure | null;

  return (
    <main className={styles.page}>
      <section className={styles.story} aria-labelledby="login-heading">
        <div className={styles.brand}>
          <span className="brand-mark" aria-hidden="true" />
          <span><strong>Photo Agent</strong><span>Web workspace</span></span>
        </div>
        <div className={styles.hero}>
          <p className={styles.eyebrow}>照片，从存下到重新发现</p>
          <h1 id="login-heading">让相册真正听懂你。</h1>
          <p>保存生活中的照片，用自然语言找回记忆，再把喜欢的瞬间变成新的作品。</p>
        </div>
        <div className={styles.capabilities} aria-label="核心能力">
          <span>智能时间线</span><span>自然语言搜索</span><span>Photo Agent</span><span>Skill 二创</span>
        </div>
      </section>

      <section className={styles.panelWrap} aria-label="账号登录">
        <div className={styles.panel}>
          <span className={styles.mode}><span className="status-dot" aria-hidden="true" />你的照片工作台</span>
          <h2>{mode === 'register' ? '创建账号' : '欢迎回来'}</h2>
          <p className={styles.intro}>{mode === 'register' ? '注册后即可开始整理你的照片。' : '登录后，继续探索你的相册。'}</p>
          <form onSubmit={submit}>
            <div className={styles.field}>
              <label htmlFor="username">账号</label>
              <input id="username" name="username" value={username} onChange={(e) => setUsername(e.target.value)}
                autoComplete="username" autoCapitalize="none" spellCheck={false} required minLength={3} maxLength={32}
                pattern="[a-zA-Z0-9_]{3,32}" aria-describedby="username-hint" disabled={mutation.isPending} />
              <small id="username-hint">3–32 位英文字母、数字或下划线，不区分大小写</small>
            </div>
            <div className={styles.field}>
              <label htmlFor="password">密码</label>
              <input id="password" name="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'register' ? 'new-password' : 'current-password'} required
                minLength={mode === 'register' ? 12 : 1} maxLength={128} disabled={mutation.isPending}
                aria-describedby={mode === 'register' ? 'password-hint' : undefined} />
              {mode === 'register' ? <small id="password-hint">12–128 位，建议使用不易猜测的长密码</small> : null}
            </div>
            {mode === 'register' ? <div className={styles.field}>
              <label htmlFor="confirmation">确认密码</label>
              <input id="confirmation" name="confirmation" type="password" value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)} autoComplete="new-password" required
                maxLength={128} disabled={mutation.isPending} />
            </div> : null}
            <button className={styles.submit} type="submit"
              disabled={mutation.isPending || (mode === 'register' && !options.data?.registration_enabled)}>
              {mutation.isPending ? '正在连接…' : mode === 'register' ? '注册并进入' : '登录'}
            </button>
          </form>
          {formError || failure ? <p className={styles.error} role="alert">
            {formError || failure?.detail || failure?.message || '登录失败，请稍后重试'}
          </p> : null}
          {options.data?.registration_enabled || mode === 'register' ? (
            <button type="button" className={styles.switchMode} onClick={switchMode} disabled={mutation.isPending}>
              {mode === 'login' ? '还没有账号？立即注册' : '已有账号？返回登录'}
            </button>
          ) : options.isError ? <p className={styles.notice}>暂时无法获取注册状态，请稍后刷新；已有账号仍可尝试登录。</p>
            : options.data ? <p className={styles.notice}>当前暂未开放新账号注册。</p> : null}
          {devLoginEnabled ? <details className={styles.development}>
            <summary>本地开发入口</summary>
            <div className={styles.field}>
              <label htmlFor="nickname">开发用户昵称</label>
              <input id="nickname" value={nickname} maxLength={64} onChange={(e) => setNickname(e.target.value)} />
            </div>
            <button type="button" className={styles.switchMode} disabled={mutation.isPending}
              onClick={() => { setFormError(''); mutation.mutate(true); }}>使用开发用户进入</button>
          </details> : null}
        </div>
      </section>
    </main>
  );
}
