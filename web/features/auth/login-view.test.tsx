import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LoginView } from './login-view';
import { getAuthOptions, loginWithPassword, registerWithPassword } from '@/lib/api/auth';
import { readSession } from '@/lib/auth/session';

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push, replace }) }));
vi.mock('@/lib/api/auth', () => ({
  getAuthOptions: vi.fn(), loginWithPassword: vi.fn(), registerWithPassword: vi.fn(), loginWithDevelopmentUser: vi.fn(),
}));
const token = { access_token: 'web-test-token', expires_in: 3600, token_type: 'bearer' };
beforeEach(() => {
  vi.clearAllMocks();
  window.sessionStorage.clear();
  vi.stubEnv('NEXT_PUBLIC_ENABLE_DEV_LOGIN', 'false');
  vi.mocked(getAuthOptions).mockResolvedValue({ registration_enabled: true, development_login_enabled: false });
  vi.mocked(loginWithPassword).mockResolvedValue(token);
  vi.mocked(registerWithPassword).mockResolvedValue(token);
});
afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

function show() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <LoginView />
  </QueryClientProvider>);
}

it('logs in with development login disabled and saves the shared session', async () => {
  show();
  fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'MY_USER' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'my long password!' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  await waitFor(() => expect(push).toHaveBeenCalledWith('/photos'));
  expect(loginWithPassword).toHaveBeenCalledWith({ username: 'my_user', password: 'my long password!' });
  expect(readSession()?.accessToken).toBe(token.access_token);
  expect(screen.queryByText('本地开发入口')).toBeNull();
  expect(screen.getByLabelText('密码')).toHaveValue('');
});

it('checks confirmation before registering and opens the workspace on success', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: '还没有账号？立即注册' }));
  fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'new_user' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'my long password!' } });
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'different password' } });
  fireEvent.click(screen.getByRole('button', { name: '注册并进入' }));
  expect(screen.getByRole('alert')).toHaveTextContent('两次输入的密码不一致');
  expect(registerWithPassword).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'my long password!' } });
  fireEvent.click(screen.getByRole('button', { name: '注册并进入' }));
  await waitFor(() => expect(push).toHaveBeenCalledWith('/photos'));
  expect(registerWithPassword).toHaveBeenCalledWith({ username: 'new_user', password: 'my long password!' });
});

it('shows credential errors without saving a session', async () => {
  vi.mocked(loginWithPassword).mockRejectedValue(Object.assign(new Error('账号或密码不正确'), { detail: '账号或密码不正确' }));
  show();
  fireEvent.change(screen.getByLabelText('账号'), { target: { value: 'my_user' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong password' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('账号或密码不正确');
  expect(readSession()).toBeNull();
  expect(push).not.toHaveBeenCalled();
});

it('hides registration when closed and hides development access in production even with the build flag', async () => {
  vi.stubEnv('NEXT_PUBLIC_ENABLE_DEV_LOGIN', 'true');
  vi.mocked(getAuthOptions).mockResolvedValue({ registration_enabled: false, development_login_enabled: false });
  show();
  expect(await screen.findByText('当前暂未开放新账号注册。')).toBeVisible();
  expect(screen.queryByRole('button', { name: '还没有账号？立即注册' })).toBeNull();
  expect(screen.queryByText('本地开发入口')).toBeNull();
  expect(screen.getByRole('button', { name: '登录' })).toBeEnabled();
});
