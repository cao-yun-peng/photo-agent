import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { WorkspaceView } from './workspace-page';
const mocks = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn(), undo: vi.fn() }));
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/api/workspace', () => ({ getWorkspace: mocks.get, updateWorkspace: mocks.update, undoWorkspace: mocks.undo }));
const state = { revision: 3, selection: [{ id: 'photo-1', version: 'a'.repeat(64), description: '杭州合照' }], task: { goal: '旅行', target_count: 12, min_group_count: 3, locked_ids: [], excluded_ids: [], prefer_landscape: false }, preferences: { preferred_subject: 'none', title_mode: 'auto' }, albums: [], facts: [], missing_selection_count: 0, memory_source: 'explicit_user', undo: { operation_id: 'op-3', expires_at: '2030-01-01' } };
beforeEach(() => { vi.clearAllMocks(); mocks.get.mockResolvedValue(state); });
afterEach(cleanup);
function show() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><WorkspaceView /></QueryClientProvider>); }
it('loads stable selection and saves the album against its visible revision', async () => {
  mocks.update.mockResolvedValue({ workspace: { ...state, revision: 4 }, operation_id: 'op-4' });
  show();
  expect(await screen.findByText('杭州合照')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('相册名称'), { target: { value: '旅行精选' } });
  fireEvent.click(screen.getByRole('button', { name: '保存为相册' }));
  await waitFor(() => expect(mocks.update).toHaveBeenCalledWith({ kind: 'save_album', title: '旅行精选' }, 3, expect.any(String)));
});
it('sends owned undo receipt and reports conflicts without assuming success', async () => {
  mocks.undo.mockRejectedValue(new Error('后续已有修改，不能撤销覆盖'));
  show();
  fireEvent.click(await screen.findByRole('button', { name: '撤销上一步' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('后续已有修改');
  expect(mocks.undo).toHaveBeenCalledWith('op-3', 3);
  expect(screen.getByText('杭州合照')).toBeInTheDocument();
});
it('writes explicit preferences only on the save action', async () => {
  mocks.update.mockResolvedValue({ workspace: { ...state, revision: 4 }, operation_id: 'op-4' });
  show();
  fireEvent.change(await screen.findByLabelText('常用选片偏好'), { target: { value: 'people' } });
  expect(mocks.update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '保存明确偏好' }));
  await waitFor(() => expect(mocks.update).toHaveBeenCalledWith({ kind: 'set_preferences', preferences: { preferred_subject: 'people', title_mode: 'auto' } }, 3, expect.any(String)));
});
