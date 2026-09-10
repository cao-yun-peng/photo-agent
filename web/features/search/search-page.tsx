/* eslint-disable @next/next/no-img-element -- URLs are short-lived backend/OSS signatures. */
'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { AddSelection } from '@/features/workspace/add-selection';
import { AppShell } from '@/components/app-shell';
import { AuthGate } from '@/components/auth-gate';
import { streamAgent, type AgentEvent, type AgentUIAction } from '@/lib/api/agent-stream';
import type { ApiFailure } from '@/lib/api/client';
import { resolveMediaUrl } from '@/lib/api/media-url';
import { reportSearchClick } from '@/lib/api/search';
import { formatPhotoDate } from '@/lib/format';
import styles from './search-page.module.css';
import { readConversations, saveConversation, removeConversation, type LocalConversation } from './conversation-storage';

const SUGGESTIONS = ['最近一周的风景', '去年冬天的雪景', '有猫的照片', '还有一张'];
const RESULT_BATCH = 24;
const SEARCH_TOOLS = new Set(['search_photos', 'continue_search', 'feedback_results', 'browse_album', 'fallback_search', 'browse_candidates']);

interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  options?: string[];
}

interface AgentPhoto {
  id: string;
  thumb_url?: string | null;
  taken_at?: string | null;
  ai_description?: string | null;
  score_semantic?: number;
  score_recency?: number;
  score_final?: number;
}

interface SearchSnapshot {
  messages: ChatMessage[];
  results: AgentPhoto[];
  input: string;
  sessionId: string | null;
  sessionExpiresAt: number;
  interrupted: boolean;
  selectedPhotoId: string | null;
  feedbackBatchId: string | null;
  undoId: string | null;
  generationReviewId: string | null;
  visibleCount: number;
  resultMode: string;
  resultTotal: number;
  resultComplete: boolean;
  coverageHint: string;
  batchLabels: Record<string, string>;
  photoReferences: Record<string, { batch: string; order: number }>;
  batchSequence: number;
}

interface ToolActivity {
  id: string;
  tool: string;
  status: 'running' | 'done' | 'failed';
  summary: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function textValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function photoItems(value: unknown): AgentPhoto[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.id !== 'string') return [];
    return [{
      id: item.id,
      thumb_url: typeof item.thumb_url === 'string' ? item.thumb_url : null,
      taken_at: typeof item.taken_at === 'string' ? item.taken_at : null,
      ai_description: typeof item.ai_description === 'string' ? item.ai_description : null,
      score_semantic: numberValue(item.score_semantic),
      score_recency: numberValue(item.score_recency),
      score_final: numberValue(item.score_final),
    }];
  });
}

function toolLabel(tool: string): string {
  return ({
    search_photos: '搜索相册',
    continue_search: '继续查找',
    feedback_results: '更新照片反馈',
    browse_album: '浏览全部相册',
    fallback_search: '扩大搜索范围',
    browse_candidates: '整理候选照片',
    ask_clarification: '确认搜索条件',
    get_photo_detail: '读取照片详情',
    recommend_skills: '查找可用 Skill',
    apply_skill: '准备图片生成',
  } as Record<string, string>)[tool] || tool || '处理请求';
}

function routeProgress(payload: Record<string, unknown>): string {
  const intent = textValue(payload.intent);
  return ({
    photo_search: '正在搜索相册…',
    search_more: '正在继续查找…',
    result_feedback: '正在更新当前结果…',
    complex_agent: '正在规划处理方式…',
  } as Record<string, string>)[intent] || '正在理解你的需求…';
}

function scorePercent(score?: number): number {
  return Math.round(Math.max(0, Math.min(1, score || 0)) * 100);
}

export function SearchPageView() {
  return (
    <AuthGate>
      {(user) => (
        <AppShell user={user}>
          <SearchWorkspace key={user.id} userId={user.id} />
        </AppShell>
      )}
    </AuthGate>
  );
}

export function SearchWorkspace({ userId = '' }: { userId?: string }) {
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [results, setResults] = useState<AgentPhoto[]>([]);
  const [visibleCount, setVisibleCount] = useState(RESULT_BATCH);
  const [resultMode, setResultMode] = useState('browse');
  const [resultTotal, setResultTotal] = useState(0);
  const [resultComplete, setResultComplete] = useState(false);
  const [coverageHint, setCoverageHint] = useState('');
  const [batchLabels, setBatchLabels] = useState<Record<string, string>>({});
  const batchSequence = useRef(0);
  const photoReferences = useRef<Record<string, { batch: string; order: number }>>({});
  const [undoId, setUndoId] = useState<string | null>(null);
  const [feedbackBatchId, setFeedbackBatchId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [selectedPhotoId, setSelectedPhotoId] = useState<string | null>(null);
  const [preview, setPreview] = useState<AgentPhoto | null>(null);
  const [generationReviewId, setGenerationReviewId] = useState<string | null>(null);
  const [progress, setProgress] = useState('');
  const [activities, setActivities] = useState<ToolActivity[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const requestSequence = useRef(0);
  const messageSequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const chatEnd = useRef<HTMLDivElement | null>(null);
  const [conversationId, setConversationId] = useState('');
  const [history, setHistory] = useState<LocalConversation<SearchSnapshot>[]>([]);
  const [historyReady, setHistoryReady] = useState(!userId);
  const [historyHint, setHistoryHint] = useState('');
  const [storageFailed, setStorageFailed] = useState(false);
  const [sessionExpiresAt, setSessionExpiresAt] = useState(0);

  const restoreConversation = useCallback((entry: LocalConversation<SearchSnapshot>) => {
    const saved = entry.snapshot;
    const canResume = Boolean(saved.sessionId && saved.sessionExpiresAt > Date.now() && !saved.interrupted);
    const restoredMessages = Array.isArray(saved.messages) ? saved.messages.filter(m => m && typeof m.id === 'string' && typeof m.text === 'string' && ['user', 'assistant', 'system'].includes(m.role)) : [];
    setConversationId(entry.id);
    setMessages(restoredMessages);
    messageSequence.current = Math.max(0, ...restoredMessages.map(m => Number(m.id.split('-').at(-1)) || 0));
    setInput(typeof saved.input === 'string' ? saved.input : '');
    setResults(photoItems(saved.results));
    setVisibleCount(Number(saved.visibleCount) || RESULT_BATCH);
    setSessionId(canResume ? saved.sessionId : null);
    setSessionExpiresAt(canResume ? saved.sessionExpiresAt : 0);
    setSelectedPhotoId(canResume ? saved.selectedPhotoId : null);
    setUndoId(canResume ? saved.undoId : null);
    setFeedbackBatchId(canResume ? saved.feedbackBatchId : null);
    setGenerationReviewId(canResume ? saved.generationReviewId : null);
    setBatchLabels(isRecord(saved.batchLabels) ? saved.batchLabels : {});
    photoReferences.current = isRecord(saved.photoReferences) ? saved.photoReferences : {};
    batchSequence.current = Number(saved.batchSequence) || 0;
    setResultMode(typeof saved.resultMode === 'string' ? saved.resultMode : 'browse');
    setResultTotal(Number(saved.resultTotal) || 0);
    setResultComplete(Boolean(saved.resultComplete));
    setCoverageHint(typeof saved.coverageHint === 'string' ? saved.coverageHint : '');
    setPreview(null); setActivities([]); setProgress(''); setFailure(null);
    setHistoryHint(canResume ? '已恢复对话，可以继续。' : saved.interrupted
      ? '上次请求被中断，记录已保留。请重新描述需求开始搜索。'
      : '历史记录已恢复。上次搜索已过期，请重新描述需求；旧照片仅供查看。');
  }, []);

  useEffect(() => {
    if (!userId) return;
    const entries = readConversations<SearchSnapshot>(userId);
    const initialize = () => {
      setHistory(entries);
      if (entries[0]) restoreConversation(entries[0]);
      else setConversationId(crypto.randomUUID());
      setHistoryReady(true);
    };
    initialize();
  }, [userId, restoreConversation]);

  useEffect(() => {
    if (!userId || !historyReady || !conversationId || (!messages.length && !input.trim())) return;
    const entry: LocalConversation<SearchSnapshot> = {
      id: conversationId,
      title: (messages.find(m => m.role === 'user')?.text || input).slice(0, 36),
      updatedAt: Date.now(),
      snapshot: { messages, results, input, sessionId, sessionExpiresAt, interrupted: streaming,
        selectedPhotoId, feedbackBatchId, undoId, generationReviewId, visibleCount,
        resultMode, resultTotal, resultComplete, coverageHint, batchLabels,
        photoReferences: photoReferences.current, batchSequence: batchSequence.current },
    };
    const save = () => {
      const ok = saveConversation(userId, entry);
      setStorageFailed(!ok);
      if (ok) setHistory(readConversations<SearchSnapshot>(userId));
    };
    save();
  }, [userId, historyReady, conversationId, messages, results, input, sessionId, sessionExpiresAt,
    streaming, selectedPhotoId, feedbackBatchId, undoId, generationReviewId, visibleCount,
    resultMode, resultTotal, resultComplete, coverageHint, batchLabels]);

  const appendMessage = useCallback((
    role: ChatMessage['role'],
    text: string,
    options?: string[],
  ) => {
    messageSequence.current += 1;
    const messageId = `agent-message-${messageSequence.current}`;
    setMessages((current) => [...current, {
      id: messageId,
      role,
      text,
      ...(options?.length ? { options } : {}),
    }]);
  }, []);

  useEffect(() => {
    chatEnd.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [messages, progress]);

  useEffect(() => {
    if (!preview) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreview(null);
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [preview]);

  useEffect(() => () => controller.current?.abort(), []);

  const handleAgentEvent = useCallback((event: AgentEvent) => {
    const payload = event.payload;
    if (event.type === 'start') {
      const incomingSession = textValue(payload.session_id);
      if (incomingSession) setSessionId(incomingSession);
      setProgress('正在理解你的需求…');
      return;
    }
    if (event.type === 'search_state') {
      if (payload.display_mode === 'replace') {
        setResults([]); setVisibleCount(RESULT_BATCH); setSelectedPhotoId(null);
        setBatchLabels({}); batchSequence.current = 0; photoReferences.current = {}; setUndoId(null);
        setPreview(null); setFeedbackBatchId(null); setResultTotal(0);
        setResultComplete(false); setCoverageHint('');
      }
      return;
    }
    if (event.type === 'route') {
      const relation = textValue(payload.relation);
      if (relation === 'new' || relation === 'replace') {
        setResults([]);
        setVisibleCount(RESULT_BATCH);
        setResultMode('browse');
        setResultTotal(0);
        setResultComplete(false);
        setCoverageHint('');
        setSelectedPhotoId(null);
        setPreview(null);
      }
      setProgress(routeProgress(payload));
      return;
    }
    if (event.type === 'think') {
      setProgress('正在规划搜索步骤…');
      return;
    }
    if (event.type === 'tool_call') {
      const tool = textValue(payload.tool, 'unknown');
      setProgress(`${toolLabel(tool)}…`);
      setActivities((current) => [...current, {
        id: `${event.step || 0}-${tool}-${current.length}`,
        tool,
        status: 'running',
        summary: '执行中',
      }]);
      return;
    }
    if (event.type === 'tool_result') {
      const tool = textValue(payload.tool, 'unknown');
      const result = isRecord(payload.result) ? payload.result : {};
      const succeeded = result.ok !== false;
      const items = photoItems(result.items);
      const summary = items.length
        ? `${items.length} 张结果`
        : textValue(result.hint, succeeded ? '已完成' : '执行失败');
      setActivities((current) => {
        const next = [...current];
        const index = next.findLastIndex((item) => item.tool === tool && item.status === 'running');
        if (index >= 0) next[index] = { ...next[index], status: succeeded ? 'done' : 'failed', summary };
        return next;
      });
      if (tool === 'apply_skill' && succeeded) {
        const id = textValue(result.generation_id);
        if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
          setGenerationReviewId(id);
        }
      }
      if (SEARCH_TOOLS.has(tool) && succeeded && Array.isArray(result.items)) {
        if (result.result_batch_id) setFeedbackBatchId(textValue(result.result_batch_id));
        const append = result.display_mode === 'append';
        if (items.length) {
          if (!append) batchSequence.current = 0;
          const batchNumber = numberValue(result.result_batch_number, ++batchSequence.current);
          if (!append) photoReferences.current = {};
          items.forEach((item, i) => {
            if (!photoReferences.current[item.id]) photoReferences.current[item.id] = { batch: textValue(result.result_batch_id), order: batchNumber * 1000000 + i };
          });
          setBatchLabels((current) => ({ ...(append ? current : {}),
            ...Object.fromEntries(items.map((item, i) => [item.id, append && current[item.id] ? current[item.id] : `第 ${batchNumber} 批 · 第 ${i + 1} 张`])) }));
        }
        setResults((current) => append
          ? [...current, ...items.filter((item) => !current.some((old) => old.id === item.id))]
          : items);
        setVisibleCount((current) => append ? current + items.length : Math.min(RESULT_BATCH, items.length));
        setResultMode(textValue(result.result_mode, 'browse'));
        setResultTotal(numberValue(result.total_matches, numberValue(result.total, items.length)));
        setResultComplete(Boolean(result.result_set_complete));
        setCoverageHint(textValue(result.coverage_hint, textValue(result.hint)));
        if (!append) setSelectedPhotoId(null);
      }
      return;
    }
    if (event.type === 'undo_available') { setUndoId(textValue(payload.undo_id) || null); return; }
    if (event.type === 'feedback_undone') {
      const restored = photoItems(payload.items);
      setResults((current) => [...current, ...restored.filter(p => !current.some(old => old.id === p.id))]
        .sort((a, b) => (photoReferences.current[a.id]?.order || 0) - (photoReferences.current[b.id]?.order || 0)));
      setResultTotal(current => current + restored.length);
      setSelectedPhotoId(textValue(payload.selected_photo_id) || null);
      setUndoId(null);
      return;
    }
    if (event.type === 'feedback') {
      setUndoId(textValue(payload.undo_id) || null);
      const removedIds = new Set(
        Array.isArray(payload.removed_photo_ids)
          ? payload.removed_photo_ids.filter((value): value is string => typeof value === 'string')
          : [],
      );
      if (removedIds.size) {
        setResults((current) => current.filter((item) => !removedIds.has(item.id)));
        setResultTotal((current) => Math.max(0, current - removedIds.size));
        setSelectedPhotoId((current) => (current && removedIds.has(current) ? null : current));
        setPreview((current) => (current && removedIds.has(current.id) ? null : current));
      }
      setProgress(Boolean(payload.continue_search) ? '正在继续查找…' : '已更新当前结果');
      return;
    }
    if (event.type === 'clarify') {
      const options = Array.isArray(payload.options)
        ? payload.options.filter((option): option is string => typeof option === 'string')
        : [];
      appendMessage('assistant', textValue(payload.question, '请补充一些照片线索。'), options);
      setProgress('');
      return;
    }
    if (event.type === 'final') {
      appendMessage('assistant', textValue(payload.message, '处理完成。'));
      setProgress('');
      return;
    }
    if (event.type === 'done') {
      setSessionExpiresAt(Date.now() + 29 * 60_000); setHistoryHint('');
      const incomingSession = textValue(payload.session_id);
      if (incomingSession) setSessionId(incomingSession);
      const state = isRecord(payload.state) ? payload.state : {};
      if ('feedback_undo' in state) setUndoId(isRecord(state.feedback_undo) ? textValue(state.feedback_undo.undo_id) || null : null);
      if ('confirmed_photo_id' in state) setSelectedPhotoId(textValue(state.confirmed_photo_id) || null);
      const confirmed = textValue(state.confirmed_photo_id);
      if (confirmed) setSelectedPhotoId(confirmed);
      setProgress('');
    }
  }, [appendMessage]);

  const runTurn = useCallback(async (query: string, selectedId?: string, uiAction?: AgentUIAction) => {
    const cleanQuery = query.trim();
    if (!cleanQuery || !historyReady || streaming || controller.current) return;
    if (userId && sessionId && sessionExpiresAt <= Date.now()) {
      setSessionId(null); setSelectedPhotoId(null); setUndoId(null); setFeedbackBatchId(null);
      setHistoryHint('记录仍在，上次搜索已过期，请重新描述需求。');
      if (selectedId || uiAction) return;
    }
    const currentRequest = ++requestSequence.current;
    const nextController = new AbortController();
    controller.current = nextController;
    setFailure(null);
    setActivities([]);
    setStreaming(true);
    setProgress(selectedId ? '正在确认你的选择…' : '正在理解你的需求…');
    appendMessage('user', cleanQuery);

    try {
      await streamAgent({
        query: cleanQuery,
        ...(uiAction ? { ui_action: uiAction } : {}),
        ...(sessionId && (!userId || sessionExpiresAt > Date.now()) ? { session_id: sessionId } : {}),
        ...(feedbackBatchId ? { feedback_batch_id: feedbackBatchId } : {}),
        ...(selectedId ? { selected_photo_id: selectedId } : {}),
      }, {
        signal: nextController.signal,
        onEvent: (event) => {
          if (requestSequence.current === currentRequest) handleAgentEvent(event);
        },
      });
    } catch (error) {
      if (requestSequence.current !== currentRequest || nextController.signal.aborted) return;
      const apiFailure = error as ApiFailure;
      const normalized = Object.assign(
        error instanceof Error ? error : new Error('Agent 执行失败'),
        {
          status: apiFailure.status || 0,
          detail: apiFailure.detail || 'Agent 执行失败，请稍后重试',
          logId: apiFailure.logId || null,
          traceId: apiFailure.traceId || null,
        },
      ) as ApiFailure;
      setFailure(normalized);
      appendMessage('assistant', normalized.detail);
      if (normalized.status === 404) { setSessionId(null); setSelectedPhotoId(null); setFeedbackBatchId(null); setUndoId(null); setSessionExpiresAt(0); setHistoryHint('记录仍在，上次搜索已过期，请重新描述需求。'); }
    } finally {
      if (requestSequence.current === currentRequest) {
        setStreaming(false);
        setProgress('');
        controller.current = null;
      }
    }
  }, [appendMessage, handleAgentEvent, sessionId, streaming, feedbackBatchId, historyReady, userId, sessionExpiresAt]);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const query = input.trim();
    if (!query) return;
    setInput('');
    void runTurn(query);
  };

  const chooseSuggestion = (suggestion: string) => {
    setInput('');
    void runTurn(suggestion);
  };

  const choosePhoto = (item: AgentPhoto, index: number) => {
    if (!sessionId || streaming) return;
    setPreview(null);
    setSelectedPhotoId(item.id);
    reportSearchClick(item.id, '', index).catch(() => undefined);
    void runTurn(`我选择第 ${index + 1} 张`, item.id);
  };

  const stop = () => {
    if (!streaming) return;
    requestSequence.current += 1;
    controller.current?.abort();
    controller.current = null;
    setStreaming(false);
    setProgress('');
    setSessionId(null); setUndoId(null);
    setActivities((current) => current.map((item) => (
      item.status === 'running' ? { ...item, status: 'failed', summary: '已停止' } : item
    )));
    appendMessage('system', '已停止本次请求。为避免未完成状态影响后续操作，下一条消息将开启新会话。');
  };

  const newConversation = () => {
    setConversationId(crypto.randomUUID()); setSessionExpiresAt(0); setHistoryHint('');
    requestSequence.current += 1;
    controller.current?.abort();
    controller.current = null;
    setInput('');
    setMessages([]);
    setGenerationReviewId(null);
    setUndoId(null); setFeedbackBatchId(null); photoReferences.current = {}; setBatchLabels({}); batchSequence.current = 0;
    setResults([]);
    setVisibleCount(RESULT_BATCH);
    setResultMode('browse');
    setResultTotal(0);
    setResultComplete(false);
    setCoverageHint('');
    setSessionId(null);
    setSelectedPhotoId(null);
    setPreview(null);
    setProgress('');
    setActivities([]);
    setStreaming(false);
    setFailure(null);
  };

  const visibleResults = results.slice(0, visibleCount);

  return (
    <div className={styles.workspace}>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>Agentic photo search</p>
          <h1>和 Photo Agent 一起找照片</h1>
          <p>描述画面，也可以继续追问、补充线索，或让 Agent 再找一张。</p>
        </div>
        <button type="button" className={styles.newConversation} onClick={newConversation}>
          新对话
        </button>
      </header>

      {userId ? <div className={styles.historyBar}>
        <label>历史对话 <select aria-label="历史对话" value={history.some(e => e.id === conversationId) ? conversationId : ''} disabled={streaming || !historyReady}
          onChange={e => { const entry = history.find(item => item.id === e.target.value); if (entry) restoreConversation(entry); }}>
          <option value="">新对话</option>
          {history.map(entry => <option key={entry.id} value={entry.id}>{entry.title}</option>)}
        </select></label>
        <span>仅保存在本浏览器，按账号保留最近20个对话</span>
        {history.some(e => e.id === conversationId) ? <button type="button" disabled={streaming} onClick={() => {
          if (removeConversation(userId, conversationId)) { setHistory(readConversations<SearchSnapshot>(userId)); newConversation(); }
          else setStorageFailed(true);
        }}>删除这条本地记录</button> : null}
        {historyHint ? <p role="status">{historyHint}</p> : null}
        {storageFailed ? <p role="alert">浏览器存储不可用或空间不足，当前对话尚未自动保存。</p> : null}
      </div> : null}
      <div className={styles.layout}>
        <section className={styles.resultPanel} aria-label="Agent 搜索结果">
          <div className={styles.resultTopline}>
            <div>
              <span>候选照片</span>
              <strong>{results.length ? `${resultTotal || results.length} 张匹配` : '等待搜索'}</strong>
            </div>
            {results.length ? (
              <span className={styles.modeBadge}>
                {resultMode === 'best' ? '最佳匹配' : resultMode === 'select' ? '由你选择' : '相关结果'}
                {resultComplete ? ' · 完整载入' : ''}
              </span>
            ) : null}
          </div>

          <p><Link href={results.length ? `/workspace?candidates=${encodeURIComponent(results.slice(0,100).map(p => p.id).join(','))}` : '/workspace'}>打开稳定选片区{results.length ? '，整理当前候选' : ''}</Link></p>

          {sessionId && feedbackBatchId ? <button type="button" className={styles.loadMore} disabled={streaming}
            onClick={() => void runTurn('再看一些', undefined, { action: 'continue_search' })}>再看一些</button> : null}
          {undoId ? <p role="status">已移除照片 · <button type="button" disabled={streaming}
            onClick={() => void runTurn('撤销刚才的移除', undefined, { action: 'undo_feedback', undo_id: undoId })}>撤销</button></p> : null}
          {coverageHint ? <p className={styles.coverageHint}>{coverageHint}</p> : null}

          {visibleResults.length ? (
            <div className={styles.grid}>
              {visibleResults.map((item, index) => {
                const imageUrl = resolveMediaUrl(item.thumb_url);
                const selected = selectedPhotoId === item.id;
                return (
                  <article className={styles.photoCard} data-selected={selected} key={item.id}>
                    <button
                      type="button"
                      className={styles.previewButton}
                      onClick={() => setPreview(item)}
                      aria-label={`查看第 ${index + 1} 张照片`}
                    >
                      {imageUrl ? <img src={imageUrl} alt="" loading="lazy" /> : <span>暂无缩略图</span>}
                      <span className={styles.rank}>{index + 1}</span>
                    </button>
                    <div className={styles.photoBody}>
                      <small>{batchLabels[item.id]}</small>
                      <p>{item.ai_description || '等待照片描述'}</p>
                      <div>
                        <span>相关 {scorePercent(item.score_final)}%</span>
                        <button type="button" disabled={streaming || !sessionId} onClick={() => choosePhoto(item, index)}>
                          {selected ? '已选择' : '选择这张'}
                        </button>
                        <button type="button" disabled={streaming || !sessionId || !batchLabels[item.id]}
                          onClick={() => { const batch = photoReferences.current[item.id]?.batch; if (batch) void runTurn(`不要${batchLabels[item.id] || '这张照片'}`, undefined, { action: 'reject_photo', photo_id: item.id, batch_id: batch }); }}>不要这张</button>
                        <AddSelection photoId={item.id} />
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className={styles.emptyResults}>
              <span aria-hidden="true">⌕</span>
              <h2>结果会在这里实时出现</h2>
              <p>先在右侧说说你想找的画面、时间、地点或人物。</p>
            </div>
          )}

          {visibleCount < results.length ? (
            <button
              type="button"
              className={styles.loadMore}
              onClick={() => setVisibleCount((count) => Math.min(count + RESULT_BATCH, results.length))}
            >
              再展示 {Math.min(RESULT_BATCH, results.length - visibleCount)} 张
            </button>
          ) : null}
        </section>

        <aside className={styles.chatPanel} aria-label="Photo Agent 对话">
          <div className={styles.chatHeader}>
            <div><span className="status-dot" aria-hidden="true" /><strong>Photo Agent</strong></div>
            <span>{sessionId ? '会话已连接' : '新会话'}</span>
          </div>

          <div className={styles.messages} aria-live="polite">
            {!messages.length ? (
              <div className={styles.welcome}>
                <strong>你想找哪一张？</strong>
                <p>我会理解你的描述、调用相册搜索，并在信息不足时向你澄清。</p>
              </div>
            ) : null}
            {generationReviewId ? <p><Link href={`/generate?generationId=${generationReviewId}`}>查看创作方案与生成任务</Link><small> 请在方案页面核对效果和费用后确认。</small></p> : null}
            {messages.map((message) => (
              <div className={styles.messageRow} data-role={message.role} key={message.id}>
                <div className={styles.messageBubble}>
                  <p>{message.text}</p>
                  {message.options?.length ? (
                    <div className={styles.clarifications}>
                      {message.options.map((option) => (
                        <button type="button" disabled={streaming} key={option} onClick={() => void runTurn(option)}>
                          {option}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              </div>
            ))}

            {activities.length ? (
              <div className={styles.activityList} aria-label="Agent 工具进度">
                {activities.map((activity) => (
                  <div data-status={activity.status} key={activity.id}>
                    <span aria-hidden="true" />
                    <strong>{toolLabel(activity.tool)}</strong>
                    <small>{activity.summary}</small>
                  </div>
                ))}
              </div>
            ) : null}

            {progress ? (
              <div className={styles.progress}>
                <span aria-hidden="true" />
                {progress}
              </div>
            ) : null}
            <div ref={chatEnd} />
          </div>

          {failure ? (
            <div className={styles.error} role="alert">
              <span>{failure.detail}</span>
              {failure.logId ? <small>Log ID: {failure.logId}</small> : null}
            </div>
          ) : null}

          <div className={styles.suggestions} aria-label="快捷提问">
            {SUGGESTIONS.map((suggestion) => (
              <button type="button" disabled={streaming} key={suggestion} onClick={() => chooseSuggestion(suggestion)}>
                {suggestion}
              </button>
            ))}
          </div>

          <form className={styles.composer} onSubmit={submit}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
              placeholder="继续描述照片、时间、地点或人物"
              aria-label="给 Photo Agent 的消息"
              rows={2}
            />
            {streaming ? (
              <button type="button" className={styles.stopButton} onClick={stop}>停止</button>
            ) : (
              <button type="submit" disabled={!input.trim()}>发送</button>
            )}
          </form>
        </aside>
      </div>

      {preview ? (
        <div className={styles.overlay} role="dialog" aria-modal="true" aria-label="候选照片预览" onClick={() => setPreview(null)}>
          <div className={styles.dialog} onClick={(event) => event.stopPropagation()}>
            {resolveMediaUrl(preview.thumb_url) ? (
              <img src={resolveMediaUrl(preview.thumb_url)!} alt={preview.ai_description || '候选照片'} />
            ) : null}
            <div>
              <button type="button" className={styles.close} onClick={() => setPreview(null)} aria-label="关闭">×</button>
              <p>{formatPhotoDate(preview.taken_at)}</p>
              <h2>{preview.ai_description || '候选照片'}</h2>
              <span>综合相关度 {scorePercent(preview.score_final)}%</span>
              <button
                type="button"
                className={styles.dialogSelect}
                disabled={streaming || !sessionId}
                onClick={() => choosePhoto(preview, Math.max(0, results.findIndex((item) => item.id === preview.id)))}
              >
                选择这张照片
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
