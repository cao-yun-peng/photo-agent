'use client';

import { useQuery } from '@tanstack/react-query';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { getCurrentUser, type CurrentUser } from '@/lib/api/auth';
import { AUTH_CHANGED_EVENT, readSession } from '@/lib/auth/session';

export function AuthGate({
  children,
}: {
  children: (user: CurrentUser) => React.ReactNode;
}) {
  const router = useRouter();
  const [hasSession, setHasSession] = useState(false);
  const [checkedSession, setCheckedSession] = useState(false);

  useEffect(() => {
    const syncSession = () => {
      const exists = Boolean(readSession());
      setHasSession(exists);
      setCheckedSession(true);
      if (!exists) router.replace('/login');
    };
    syncSession();
    window.addEventListener(AUTH_CHANGED_EVENT, syncSession);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, syncSession);
  }, [router]);

  const userQuery = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: getCurrentUser,
    enabled: checkedSession && hasSession,
    retry: false,
  });

  useEffect(() => {
    if (userQuery.isError) router.replace('/login');
  }, [router, userQuery.isError]);

  if (!checkedSession || (hasSession && userQuery.isPending)) {
    return (
      <main className="grid min-h-screen place-items-center px-6">
        <div className="flex items-center gap-3 text-sm text-[var(--muted)]">
          <span className="status-dot" aria-hidden="true" />
          正在连接 Photo Agent…
        </div>
      </main>
    );
  }

  if (!hasSession || !userQuery.data) return null;
  return children(userQuery.data);
}
