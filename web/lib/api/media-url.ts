import { API_ORIGIN } from './client';

export function resolveMediaUrl(value: string | null | undefined): string | null {
  if (!value) return null;

  try {
    const absoluteUrl = new URL(value);
    if (absoluteUrl.protocol === 'http:' || absoluteUrl.protocol === 'https:') {
      return absoluteUrl.toString();
    }
  } catch {
    // Relative mock-object paths are resolved against the configured API base below.
  }

  const apiOrigin = API_ORIGIN.replace(/\/$/, '');
  const mediaPath = value.replace(/^\/+/, '');
  return `${apiOrigin}/${mediaPath}`;
}
