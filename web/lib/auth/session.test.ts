import { beforeEach, describe, expect, it, vi } from 'vitest';
import { clearSession, readSession, saveSession } from './session';

describe('web session', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('stores and restores a non-expired token', () => {
    vi.spyOn(Date, 'now').mockReturnValue(1_000);
    saveSession('token-123', 60);

    expect(readSession()).toEqual({
      accessToken: 'token-123',
      expiresAt: 61_000,
    });
    vi.restoreAllMocks();
  });

  it('drops an expired token', () => {
    window.sessionStorage.setItem(
      'photo-agent:web-session',
      JSON.stringify({ accessToken: 'expired', expiresAt: 1 }),
    );

    expect(readSession()).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it('clears the current token', () => {
    saveSession('token-123', 60);
    clearSession();
    expect(readSession()).toBeNull();
  });
});
