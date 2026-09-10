'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState, useSyncExternalStore } from 'react';
import { sessionEpoch, subscribeSession } from '@/lib/auth/session';

function SessionQueries({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: 1, staleTime: 30_000 }, mutations: { retry: 0 } },
  }));
  useEffect(() => () => {
    void client.cancelQueries();
    client.clear();
  }, [client]);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const epoch = useSyncExternalStore(subscribeSession, sessionEpoch, () => 0);
  return <SessionQueries key={epoch}>{children}</SessionQueries>;
}
