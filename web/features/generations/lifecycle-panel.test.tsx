import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { LifecyclePanel } from './lifecycle-panel';
import type { Generation } from '@/lib/api/generations';
const mocks = vi.hoisted(() => ({ cancel: vi.fn(), iterate: vi.fn(), cost: vi.fn().mockResolvedValue({ call_count: 3, estimated_yuan_known: 0.15, unknown_estimate_count: 1 }) }));
vi.mock('@/lib/api/generations', () => ({ getGenerationCost: mocks.cost, cancelGeneration: mocks.cancel, iterateGeneration: mocks.iterate, createIdempotencyKey: () => 'iteration-key' }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
function show(status: string) {
  const changed = vi.fn();
  const gen = { id: 'gen', status, progress_stage: status, execution_snapshot: {}, iteration_index: 0 } as Generation;
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}><LifecyclePanel generation={gen} onChange={changed} /></QueryClientProvider>);
  return changed;
}
it('requests cancellation and displays the returned state', async () => {
  mocks.cancel.mockResolvedValue({ id: 'gen', status: 'cancel_requested' });
  const changed = show('processing');
  fireEvent.click(screen.getByRole('button', { name: '取消本次任务' }));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(mocks.cancel).toHaveBeenCalledWith('gen');
});
it('creates a reviewable iteration only after feedback is submitted', async () => {
  mocks.iterate.mockResolvedValue({ id: 'new', status: 'awaiting_confirmation' });
  const changed = show('done');
  expect(screen.getByRole('button', { name: '预览调整方案' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('结果调整要求'), { target: { value: '保留表情，增加留白' } });
  fireEvent.click(screen.getByRole('button', { name: '预览调整方案' }));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(mocks.iterate).toHaveBeenCalledWith('gen', '保留表情，增加留白', 'iteration-key');
  expect(mocks.cancel).not.toHaveBeenCalled();
});
it('never offers a new iteration for an uncertain provider outcome', () => {
  show('outcome_unknown');
  expect(screen.getByText(/供应商可能仍在处理或已计费/)).toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('shows known estimate separately from unknown costs and billing', async () => {
  show('done');
  expect(await screen.findByText(/另有 1 次费用未知/)).toHaveTextContent('尚未与账单核对');
});
