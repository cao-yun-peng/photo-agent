import { describe, expect, it } from 'vitest';
import { formatBytes, formatPhotoDate } from './format';

describe('display formatters', () => {
  it('formats byte sizes for the upload queue', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB');
  });

  it('handles absent or invalid photo dates', () => {
    expect(formatPhotoDate(null)).toBe('时间未知');
    expect(formatPhotoDate('not-a-date')).toBe('时间未知');
  });
});
