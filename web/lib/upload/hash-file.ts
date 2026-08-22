export function hashFile(
  file: File,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../../workers/hash.worker.ts', import.meta.url), {
      type: 'module',
    });

    const stop = () => worker.terminate();
    const abort = () => {
      stop();
      reject(new DOMException('Hashing aborted', 'AbortError'));
    };

    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener('abort', abort, { once: true });

    worker.onmessage = (
      event: MessageEvent<
        | { type: 'progress'; progress: number }
        | { type: 'done'; hash: string }
        | { type: 'error'; message: string }
      >,
    ) => {
      if (event.data.type === 'progress') {
        onProgress(event.data.progress);
        return;
      }
      signal?.removeEventListener('abort', abort);
      stop();
      if (event.data.type === 'done') resolve(event.data.hash);
      else reject(new Error(event.data.message));
    };

    worker.onerror = (event) => {
      signal?.removeEventListener('abort', abort);
      stop();
      reject(new Error(event.message || '文件指纹计算失败'));
    };

    worker.postMessage({ file });
  });
}
