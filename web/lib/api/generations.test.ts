import { describe, expect, it } from 'vitest';
import {
  createIdempotencyKey,
  requiresGenerationConfirmation,
  shouldPollGeneration,
} from './generations';

describe('generation state helpers', () => {
  it('polls only queued or processing tasks', () => {
    expect(shouldPollGeneration('pending')).toBe(true);
    expect(shouldPollGeneration('processing')).toBe(true);
    expect(shouldPollGeneration('awaiting_confirmation')).toBe(false);
    expect(shouldPollGeneration('queue_failed')).toBe(false);
    expect(shouldPollGeneration('done')).toBe(false);
    expect(shouldPollGeneration('failed')).toBe(false);
  });

  it('creates backend-compatible unique idempotency keys', () => {
    const first = createIdempotencyKey();
    const second = createIdempotencyKey();
    expect(first.length).toBeGreaterThanOrEqual(8);
    expect(first.length).toBeLessThanOrEqual(128);
    expect(first).not.toBe(second);
  });

  it('requires confirmation for new and retryable enqueue states', () => {
    expect(requiresGenerationConfirmation('awaiting_confirmation')).toBe(true);
    expect(requiresGenerationConfirmation('queue_failed')).toBe(true);
    expect(requiresGenerationConfirmation('pending')).toBe(false);
    expect(requiresGenerationConfirmation('done')).toBe(false);
  });
});
