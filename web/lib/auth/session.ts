export const AUTH_CHANGED_EVENT = 'photo-agent:auth-changed';
let epoch = 0;
let controller: AbortController | undefined;
export function sessionEpoch() { return epoch; }
export function sessionSignal() { return (controller ??= new AbortController()).signal; }
function changed() {
  epoch += 1;
  controller?.abort();
  controller = new AbortController();
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}
export function subscribeSession(listener: () => void) {
  window.addEventListener(AUTH_CHANGED_EVENT, listener);
  return () => window.removeEventListener(AUTH_CHANGED_EVENT, listener);
}
export function clearSessionIfCurrent(token: string | undefined, expectedEpoch: number) {
  if (token && expectedEpoch === epoch && readSession()?.accessToken === token) clearSession();
}
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
  changed();
  return session;
}

export function clearSession(): void {
  if (typeof window === 'undefined') return;
  if (window.sessionStorage.getItem(SESSION_KEY) === null) return;
  window.sessionStorage.removeItem(SESSION_KEY);
  changed();
}
