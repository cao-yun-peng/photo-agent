export function putFile({
  url,
  file,
  headers,
  onProgress,
  signal,
}: {
  url: string;
  file: File;
  headers: Record<string, string>;
  onProgress: (progress: number) => void;
  signal?: AbortSignal;
}): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    Object.entries(headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));

    const abort = () => xhr.abort();
    signal?.addEventListener('abort', abort, { once: true });
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      signal?.removeEventListener('abort', abort);
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`对象存储上传失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error('对象存储网络连接失败'));
    xhr.onabort = () => reject(new DOMException('Upload aborted', 'AbortError'));
    xhr.send(file);
  });
}
