import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
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
  beforeEach(() => {
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
    });
    expect(await screen.findByText('已确认这张照片。')).toBeInTheDocument();
  });
});
