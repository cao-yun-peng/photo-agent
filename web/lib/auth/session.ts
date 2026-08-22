export const AUTH_CHANGED_EVENT = 'photo-agent:auth-changed';
const SESSION_KEY = 'photo-agent:web-session';

export interface WebSession {
  accessToken: string;
  expiresAt: number;
}

export function readSession(): WebSession | null {
  if (typeof window === 'undefined') return null;

  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as WebSession;
    if (!session.accessToken || session.expiresAt <= Date.now()) {
      clearSession();
      return null;
    }
    return session;
  } catch {
    clearSession();
    return null;
  }
}

export function saveSession(accessToken: string, expiresIn: number): WebSession {
  const session = {
    accessToken,
    expiresAt: Date.now() + expiresIn * 1_000,
  };
  window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
  return session;
}

export function clearSession(): void {
  if (typeof window === 'undefined') return;
  window.sessionStorage.removeItem(SESSION_KEY);
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}
