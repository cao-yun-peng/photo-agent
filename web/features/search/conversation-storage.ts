export interface LocalConversation<T> {
  id: string;
  title: string;
  updatedAt: number;
  snapshot: T;
}

const key = (userId: string) => `photo-agent:conversations:v1:${userId}`;
export const LOCAL_CONVERSATION_LIMIT = 20;

export function readConversations<T>(userId: string): LocalConversation<T>[] {
  if (!userId || typeof window === 'undefined') return [];
  try {
    const data: unknown = JSON.parse(window.localStorage.getItem(key(userId)) || '[]');
    if (!Array.isArray(data)) return [];
    return data.filter((entry) => entry && typeof entry.id === 'string'
      && typeof entry.title === 'string' && Number.isFinite(entry.updatedAt)
      && entry.snapshot && typeof entry.snapshot === 'object').slice(0, LOCAL_CONVERSATION_LIMIT);
  } catch { return []; }
}

export function saveConversation<T>(userId: string, entry: LocalConversation<T>): boolean {
  if (!userId || typeof window === 'undefined') return false;
  try {
    const entries = [entry, ...readConversations<T>(userId).filter(old => old.id !== entry.id)]
      .slice(0, LOCAL_CONVERSATION_LIMIT);
    window.localStorage.setItem(key(userId), JSON.stringify(entries));
    return true;
  } catch { return false; }
}

export function removeConversation(userId: string, id: string): boolean {
  try {
    window.localStorage.setItem(key(userId), JSON.stringify(readConversations(userId).filter(entry => entry.id !== id)));
    return true;
  } catch { return false; }
}
