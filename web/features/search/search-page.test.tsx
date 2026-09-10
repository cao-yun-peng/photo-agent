import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SearchWorkspace } from './search-page';

const mocks = vi.hoisted(() => ({
  streamAgent: vi.fn(),
  reportSearchClick: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/lib/api/agent-stream', () => ({
  streamAgent: mocks.streamAgent,
}));

vi.mock('@/lib/api/search', () => ({
  reportSearchClick: mocks.reportSearchClick,
}));

describe('Agent search workspace', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    window.localStorage.clear();
    mocks.streamAgent.mockReset();
    mocks.reportSearchClick.mockClear();
    Element.prototype.scrollIntoView = vi.fn();
  });

  it('continues the same session when the user selects a returned photo', async () => {
    mocks.streamAgent
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({
          type: 'tool_result',
          payload: {
            tool: 'search_photos',
            result: {
              ok: true,
              items: [{
                id: 'photo-1',
                ai_description: '山谷里的日落',
                score_final: 0.92,
              }],
              result_batch_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              total_matches: 1,
              result_mode: 'select',
            },
          },
        });
        options.onEvent({ type: 'final', payload: { message: '找到一张候选照片。' } });
        options.onEvent({
          type: 'done',
          payload: { session_id: 'session-1', status: 'completed', state: {} },
        });
        return [];
      })
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({ type: 'final', payload: { message: '已确认这张照片。' } });
        options.onEvent({
          type: 'done',
          payload: {
            session_id: 'session-1',
            status: 'completed',
            state: { confirmed_photo_id: 'photo-1' },
          },
        });
        return [];
      });

    render(<SearchWorkspace />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), {
      target: { value: '找山里的日落' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByText('山谷里的日落')).toBeInTheDocument();
    const select = screen.getByRole('button', { name: '选择这张' });
    await waitFor(() => expect(select).toBeEnabled());
    fireEvent.click(select);

    await waitFor(() => expect(mocks.streamAgent).toHaveBeenCalledTimes(2));
    expect(mocks.streamAgent.mock.calls[1][0]).toEqual({
      query: '我选择第 1 张',
      session_id: 'session-1',
      selected_photo_id: 'photo-1',
      feedback_batch_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    });
    expect(await screen.findByText('已确认这张照片。')).toBeInTheDocument();
  });

  it('clears stale photos as soon as a replacement search is routed', async () => {
    let releaseSecondTurn: (() => void) | undefined;
    mocks.streamAgent
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({
          type: 'tool_result',
          payload: {
            tool: 'search_photos',
            result: {
              ok: true,
              items: [{ id: 'photo-cat', ai_description: '窗台上的猫' }],
              total_matches: 1,
            },
          },
        });
        options.onEvent({ type: 'final', payload: { message: '找到猫的照片。' } });
        options.onEvent({ type: 'done', payload: { session_id: 'session-1', state: {} } });
        return [];
      })
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({
          type: 'route',
          payload: { intent: 'photo_search', relation: 'replace' },
        });
        await new Promise<void>((resolve) => { releaseSecondTurn = resolve; });
        options.onEvent({ type: 'final', payload: { message: '没有找到狗的照片。' } });
        return [];
      });

    render(<SearchWorkspace />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), {
      target: { value: '找猫的照片' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('窗台上的猫')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), {
      target: { value: '不要猫了，找狗的照片' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(mocks.streamAgent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('窗台上的猫')).not.toBeInTheDocument());
    releaseSecondTurn?.();
  });

  it('removes only the explicitly rejected photo from current results', async () => {
    mocks.streamAgent
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({
          type: 'tool_result',
          payload: {
            tool: 'search_photos',
            result: {
              ok: true,
              items: [
                { id: 'photo-1', ai_description: '第一张猫照片' },
                { id: 'photo-2', ai_description: '第二张猫照片' },
              ],
              total_matches: 2,
            },
          },
        });
        options.onEvent({ type: 'final', payload: { message: '找到两张。' } });
        options.onEvent({ type: 'done', payload: { session_id: 'session-1', state: {} } });
        return [];
      })
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({
          type: 'feedback',
          payload: { removed_photo_ids: ['photo-2'], continue_search: false },
        });
        options.onEvent({ type: 'final', payload: { message: '已移除第 2 张。' } });
        return [];
      });

    render(<SearchWorkspace />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), {
      target: { value: '找猫的照片' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByText('第二张猫照片')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), {
      target: { value: '第2张不需要' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(screen.queryByText('第二张猫照片')).not.toBeInTheDocument());
    expect(screen.getByText('第一张猫照片')).toBeInTheDocument();
  });
  it('links an Agent generation to review without confirming it in chat', async () => {
    const id = '11111111-2222-4333-8444-555555555555';
    mocks.streamAgent.mockImplementation(async (_request, options) => {
      options.onEvent({ type: 'tool_result', payload: { tool: 'apply_skill', result: {
        ok: true, generation_id: id, status: 'awaiting_confirmation', confirmation_required: true,
      } } });
      options.onEvent({ type: 'final', payload: { message: '请查看创作方案。' } });
      return [];
    });
    render(<SearchWorkspace />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '用针织 Skill，不要文字' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('link', { name: '查看创作方案与生成任务' })).toHaveAttribute('href', `/generate?generationId=${id}`);
    expect(mocks.streamAgent).toHaveBeenCalledTimes(1);
  });

  it('appends unique continuation photos and replaces them when the goal changes even on failure', async () => {
    mocks.streamAgent
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'start', payload: { session_id: 'session-1' } });
        options.onEvent({ type: 'search_state', payload: { display_mode: 'replace', search_goal_id: 'cats' } });
        options.onEvent({ type: 'tool_result', payload: { tool: 'search_photos', result: {
          ok: true, display_mode: 'replace', items: [{ id: 'a', ai_description: '第一批猫' }], result_batch_id: 'batch-a',
        } } });
      })
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'tool_result', payload: { tool: 'continue_search', result: {
          ok: true, display_mode: 'append', items: [{ id: 'a', ai_description: '第一批猫' }, { id: 'b', ai_description: '第二批猫' }], result_batch_id: 'batch-b',
        } } });
      })
      .mockImplementationOnce(async (_request, options) => {
        options.onEvent({ type: 'search_state', payload: { display_mode: 'replace', search_goal_id: 'dogs' } });
        options.onEvent({ type: 'tool_result', payload: { tool: 'search_photos', result: { ok: false, error_type: 'timeout' } } });
      });
    render(<SearchWorkspace />);
    const send = async (query: string, count: number) => {
      await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeEnabled());
      fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: query } });
      fireEvent.click(screen.getByRole('button', { name: '发送' }));
      await waitFor(() => expect(mocks.streamAgent).toHaveBeenCalledTimes(count));
    };
    // The empty input disables send; enter text before waiting for the button.
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '猫' } });
    await send('猫', 1);
    expect(await screen.findByText('第一批猫')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '还有吗' } });
    await send('还有吗', 2);
    expect(await screen.findByText('第二批猫')).toBeInTheDocument();
    expect(screen.getAllByText('第一批猫')).toHaveLength(1);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '换成狗' } });
    await send('换成狗', 3);
    await waitFor(() => expect(screen.queryByText('第一批猫')).not.toBeInTheDocument());
    expect(screen.queryByText('第二批猫')).not.toBeInTheDocument();
  });


  it('rejects exact photos and restores selection through undo', async () => {
    const a = { id: 'a', ai_description: '第一只猫' };
    const b = { id: 'b', ai_description: '第二只猫' };
    mocks.streamAgent.mockImplementation(async (request, { onEvent }) => {
      const emit = (type: string, payload: unknown) => onEvent({ type, payload });
      if (!request.ui_action) {
        emit('start', { session_id: 'session' });
        emit('tool_result', { tool: 'search_photos', result: { ok: true, items: [a, b], result_batch_id: 'batch', result_batch_number: 1 } });
        emit('done', { state: { confirmed_photo_id: 'b' } });
      } else if (request.ui_action.action === 'reject_photo') {
        emit('feedback', { removed_photo_ids: ['b'], undo_id: 'undo' });
        emit('done', { state: { confirmed_photo_id: null, feedback_undo: { undo_id: 'undo' } } });
      } else {
        emit('feedback_undone', { items: [b], selected_photo_id: 'b' });
        emit('done', { state: { confirmed_photo_id: 'b', feedback_undo: null } });
      }
      return [];
    });
    render(<SearchWorkspace />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '找猫' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('第二只猫');
    await waitFor(() => expect(screen.getAllByRole('button', { name: '不要这张' })[1]).toBeEnabled());
    fireEvent.click(screen.getAllByRole('button', { name: '不要这张' })[1]);
    await waitFor(() => expect(screen.queryByText('第二只猫')).not.toBeInTheDocument());
    expect(mocks.streamAgent.mock.calls[1][0].ui_action).toEqual({ action: 'reject_photo', photo_id: 'b', batch_id: 'batch' });
    await waitFor(() => expect(screen.getByRole('button', { name: '撤销' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '撤销' }));
    await screen.findByText('第二只猫');
    expect(screen.queryByRole('button', { name: '撤销' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '已选择' })).toBeInTheDocument();
    expect(mocks.streamAgent.mock.calls[2][0].ui_action).toEqual({ action: 'undo_feedback', undo_id: 'undo' });
  });

  it('restores messages, photos, draft and session after leaving the page', async () => {
    mocks.streamAgent.mockImplementation(async (_request, { onEvent }) => {
      onEvent({ type: 'start', payload: { session_id: 'saved-session' } });
      onEvent({ type: 'tool_result', payload: { tool: 'search_photos', result: { ok: true, items: [{ id: 'a', ai_description: '保存的猫照片' }], result_batch_id: 'batch' } } });
      onEvent({ type: 'final', payload: { message: '已找到照片' } });
      onEvent({ type: 'done', payload: { session_id: 'saved-session', state: {} } });
    });
    const first = render(<SearchWorkspace userId="user-a" />);
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '寻找猫咪' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('保存的猫照片');
    await waitFor(() => expect(screen.getByRole('button', { name: '选择这张' })).toBeEnabled());
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '还没发出的草稿' } });
    first.unmount();
    const second = render(<SearchWorkspace userId="user-a" />);
    expect(await screen.findByText('保存的猫照片')).toBeInTheDocument();
    expect(screen.getByText('已找到照片')).toBeInTheDocument();
    expect(screen.getByLabelText('给 Photo Agent 的消息')).toHaveValue('还没发出的草稿');
    fireEvent.change(screen.getByLabelText('给 Photo Agent 的消息'), { target: { value: '还有吗' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(mocks.streamAgent).toHaveBeenCalledTimes(2));
    expect(mocks.streamAgent.mock.calls[1][0].session_id).toBe('saved-session');
    second.unmount();
    render(<SearchWorkspace userId="user-b" />);
    expect(screen.queryByText('保存的猫照片')).not.toBeInTheDocument();
    expect(screen.getByLabelText('给 Photo Agent 的消息')).toHaveValue('');
  });

  it.each([false, true])('keeps expired or interrupted history read-only: interrupted=%s', async (interrupted) => {
    localStorage.setItem('photo-agent:conversations:v1:user-a', JSON.stringify([{ id: 'old', title: '以前的猫', updatedAt: Date.now(), snapshot: {
      messages: [{ id: 'agent-message-7', role: 'user', text: '以前找过猫' }],
      results: [{ id: 'a', ai_description: '旧照片' }], input: '', sessionId: 'expired',
      sessionExpiresAt: interrupted ? Date.now() + 999999 : 1, interrupted, selectedPhotoId: 'a', undoId: 'old-undo',
    } }]));
    render(<SearchWorkspace userId="user-a" />);
    expect(await screen.findByText('以前找过猫', { selector: 'p' })).toBeInTheDocument();
    expect(screen.getByText('旧照片')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '选择这张' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '撤销' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(screen.queryByText('旧照片')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('历史对话'), { target: { value: 'old' } });
    expect(await screen.findByText('旧照片')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除这条本地记录' }));
    expect(screen.queryByText('旧照片')).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem('photo-agent:conversations:v1:user-a') || '[]')).toHaveLength(0);
  });
});
