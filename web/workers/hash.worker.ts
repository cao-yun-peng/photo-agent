/// <reference lib="webworker" />

import { createSHA256 } from 'hash-wasm';

const CHUNK_SIZE = 4 * 1024 * 1024;

self.onmessage = async (event: MessageEvent<{ file: File }>) => {
  const { file } = event.data;
  try {
    const hasher = await createSHA256();
    hasher.init();

    for (let offset = 0; offset < file.size; offset += CHUNK_SIZE) {
      const chunk = file.slice(offset, Math.min(offset + CHUNK_SIZE, file.size));
      hasher.update(new Uint8Array(await chunk.arrayBuffer()));
      self.postMessage({
        type: 'progress',
        progress: Math.min(1, (offset + chunk.size) / file.size),
      });
    }

    self.postMessage({ type: 'done', hash: hasher.digest('hex') });
  } catch (error) {
    self.postMessage({
      type: 'error',
      message: error instanceof Error ? error.message : String(error),
    });
  }
};
