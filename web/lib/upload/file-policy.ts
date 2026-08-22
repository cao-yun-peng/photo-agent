export const MAX_IMAGE_BYTES = 100 * 1024 * 1024;

export const IMAGE_MIME_BY_EXTENSION: Record<string, string> = {
  gif: 'image/gif',
  heic: 'image/heic',
  heif: 'image/heif',
  jpeg: 'image/jpeg',
  jpg: 'image/jpeg',
  png: 'image/png',
  webp: 'image/webp',
};

const ALLOWED_IMAGE_MIME_TYPES = new Set(Object.values(IMAGE_MIME_BY_EXTENSION));

export function resolveImageMimeType(file: Pick<File, 'name' | 'type'>): string | null {
  if (ALLOWED_IMAGE_MIME_TYPES.has(file.type.toLowerCase())) return file.type.toLowerCase();
  const extension = file.name.split('.').pop()?.toLowerCase();
  return extension ? IMAGE_MIME_BY_EXTENSION[extension] || null : null;
}

export function validateImageFile(
  file: Pick<File, 'name' | 'size' | 'type'>,
): { mimeType: string | null; error: string | null } {
  const mimeType = resolveImageMimeType(file);
  if (!mimeType) return { mimeType: null, error: '仅支持 JPG、PNG、WebP、HEIC、HEIF 或 GIF' };
  if (file.size <= 0) return { mimeType, error: '文件内容为空' };
  if (file.size > MAX_IMAGE_BYTES) return { mimeType, error: '单张照片不能超过 100 MB' };
  return { mimeType, error: null };
}
