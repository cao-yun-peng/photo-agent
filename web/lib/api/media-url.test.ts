import { describe, expect, it } from 'vitest';
import { API_ORIGIN } from './client';
import { resolveMediaUrl } from './media-url';

describe('media URL resolution', () => {
  it('resolves mock object-storage paths against the API origin', () => {
    expect(resolveMediaUrl('/_mock/oss/photo.jpg')).toBe(`${API_ORIGIN}/_mock/oss/photo.jpg`);
  });

  it('keeps absolute signed URLs and handles empty values', () => {
    expect(resolveMediaUrl('https://cdn.example.test/a.jpg?token=1'))
      .toBe('https://cdn.example.test/a.jpg?token=1');
    expect(resolveMediaUrl(null)).toBeNull();
  });
});
