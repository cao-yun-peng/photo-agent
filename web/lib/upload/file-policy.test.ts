import { describe, expect, it } from 'vitest';
import { MAX_IMAGE_BYTES, resolveImageMimeType, validateImageFile } from './file-policy';

describe('image upload policy', () => {
  it('accepts a supported browser mime type', () => {
    expect(validateImageFile({ name: 'photo.jpg', size: 42, type: 'image/jpeg' })).toEqual({
      mimeType: 'image/jpeg',
      error: null,
    });
  });

  it('infers HEIC when a browser omits the mime type', () => {
    expect(resolveImageMimeType({ name: 'IMG_0001.HEIC', type: '' })).toBe('image/heic');
  });

  it('rejects unsupported and oversized files', () => {
    expect(validateImageFile({ name: 'notes.txt', size: 10, type: 'text/plain' }).error)
      .toContain('仅支持');
    expect(validateImageFile({ name: 'huge.png', size: MAX_IMAGE_BYTES + 1, type: 'image/png' }).error)
      .toContain('100 MB');
  });
});
