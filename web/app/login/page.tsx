import type { Metadata } from 'next';
import { LoginView } from '@/features/auth/login-view';

export const metadata: Metadata = { title: '开发态登录' };

export default function LoginPage() {
  return <LoginView />;
}
