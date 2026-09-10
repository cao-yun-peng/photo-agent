import { beforeEach, expect, it, vi } from 'vitest';
import { readConversations, removeConversation, saveConversation } from './conversation-storage';

beforeEach(() => localStorage.clear());
const entry = (id: string) => ({ id, title: id, updatedAt: Date.now(), snapshot: { messages: [] } });

it('isolates accounts and retains the latest twenty conversations', () => {
  for (let i = 0; i < 25; i++) expect(saveConversation('a', entry(String(i)))).toBe(true);
  expect(readConversations('a')).toHaveLength(20);
  expect(readConversations('a')[0].id).toBe('24');
  expect(readConversations('b')).toEqual([]);
  expect(removeConversation('a', '24')).toBe(true);
  expect(readConversations('a')[0].id).toBe('23');
});

it('handles malformed storage and reports quota failures', () => {
  localStorage.setItem('photo-agent:conversations:v1:a', '{broken');
  expect(readConversations('a')).toEqual([]);
  const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
  try { expect(saveConversation('a', entry('one'))).toBe(false); } finally { spy.mockRestore(); }
});
