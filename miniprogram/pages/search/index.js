// pages/search/index.js
const { agent, search, generations, API_BASE } = require('../../utils/api');

const recorder = wx.getRecorderManager();
let voicePath = null;
let messageSequence = 0;
let completeResultBuffer = [];
const RESULT_RENDER_BATCH = 30;

Page({
  data: {
    q: '',
    quickChips: ['最近一周', '上个月', '美食', '风景', '人像'],
    activeChip: '',
    results: [],
    parsed: null,
    loading: false,
    resultMode: 'browse',
    selectedResultId: '',
    resultTotal: 0,
    resultSetComplete: false,
    coverageHint: '',
    hasMoreResults: false,
    recording: false,
    apiBase: API_BASE,
    agentSessionId: '',
    agentStatus: '',
    agentProgress: '',
    awaitingClarification: false,
    chatMessages: [],
    chatScrollTarget: '',
    searchInputFocused: false,
  },

  onShow() {
    const app = getApp();
    if (!app.isLoggedIn()) wx.reLaunch({ url: '/pages/login/index' });
  },

  onQChange(e) {
    this.setData({ q: e.detail.value });
  },

  onTapChip(e) {
    const chip = e.currentTarget.dataset.chip;
    this.setData({ q: chip, activeChip: chip }, () => this._runSearch('browse'));
  },

  onTapClarificationOption(e) {
    const option = e.currentTarget.dataset.option;
    this.setData({
      q: option + '，',
      activeChip: '',
      searchInputFocused: true,
    });
  },

  onSearch() {
    return this._runSearch('browse');
  },

  onSearchBest() {
    return this._runSearch('best');
  },

  onNewConversation() {
    if (this.data.loading) return;
    completeResultBuffer = [];
    this.setData({
      q: '',
      activeChip: '',
      results: [],
      parsed: null,
      resultMode: 'browse',
      selectedResultId: '',
      resultTotal: 0,
      resultSetComplete: false,
      coverageHint: '',
      hasMoreResults: false,
      agentSessionId: '',
      agentStatus: '',
      agentProgress: '',
      awaitingClarification: false,
      chatMessages: [],
      chatScrollTarget: '',
      searchInputFocused: true,
    });
  },

  async _runSearch(resultMode) {
    if (this.data.loading) return;
    const q = (this.data.q || '').trim();
    if (!q) {
      wx.showToast({ title: '请输入或说一句话', icon: 'none' });
      return;
    }

    const agentQuery = resultMode === 'best' ? `${q}，请只选最好的一张` : q;
    const sessionId = this.data.agentSessionId || null;
    this._appendChatMessage('user', resultMode === 'best' ? `${q}（只选最好的一张）` : q);
    this.setData({
      q: '',
      activeChip: '',
      loading: true,
      resultMode,
      agentProgress: '正在理解你的需求…',
      awaitingClarification: false,
      searchInputFocused: false,
    });

    try {
      await agent.stream({
        query: agentQuery,
        session_id: sessionId,
        onEvent: (event) => this._handleAgentEvent(event, resultMode),
      });
    } catch (err) {
      const detail = typeof err.detail === 'string' ? err.detail : 'Agent 执行失败';
      this._appendChatMessage('assistant', detail);
      if (err.status === 404) {
        this.setData({ agentSessionId: '', agentStatus: '' });
      }
      wx.showToast({ title: detail.slice(0, 20), icon: 'none' });
    } finally {
      this.setData({ loading: false, agentProgress: '' });
    }
  },

  _handleAgentEvent(event, requestedMode) {
    const payload = event.payload || {};
    if (event.type === 'start') {
      this.setData({
        agentSessionId: payload.session_id || this.data.agentSessionId,
        agentProgress: '正在理解你的需求…',
      });
      return;
    }
    if (event.type === 'route') {
      const routeLabels = {
        photo_search: '正在搜索相册…',
        search_more: '正在继续查找…',
        complex_agent: '正在规划处理方式…',
      };
      this.setData({ agentProgress: routeLabels[payload.intent] || '正在理解你的需求…' });
      return;
    }
    if (event.type === 'think') {
      this.setData({ agentProgress: '正在规划搜索…' });
      return;
    }
    if (event.type === 'tool_call') {
      const labels = {
        search_photos: '正在搜索照片…',
        fallback_search: '正在扩大范围查找…',
        browse_candidates: '正在整理候选照片…',
        ask_clarification: '正在确认搜索条件…',
      };
      this.setData({ agentProgress: labels[payload.tool] || '正在处理…' });
      return;
    }
    if (event.type === 'tool_result') {
      const result = payload.result || {};
      if (payload.tool === 'apply_skill' && result.confirmation_required) {
        this._confirmAgentGeneration(result);
      }
      if (['search_photos', 'fallback_search', 'browse_candidates'].includes(payload.tool)) {
        completeResultBuffer = (result.items || []).map((item) => ({
          ...item,
          thumb_url_full: this._resolveThumb(item.thumb_url),
        }));
        const visibleCount = Math.min(RESULT_RENDER_BATCH, completeResultBuffer.length);
        this.setData({
          results: completeResultBuffer.slice(0, visibleCount),
          parsed: result.parsed || this.data.parsed,
          resultMode: result.result_mode || requestedMode,
          selectedResultId: '',
          resultTotal: Number(
            result.total_matches !== undefined
              ? result.total_matches
              : completeResultBuffer.length,
          ),
          resultSetComplete: Boolean(result.result_set_complete),
          coverageHint: result.coverage_hint || '',
          hasMoreResults: visibleCount < completeResultBuffer.length,
        });
      }
      return;
    }
    if (event.type === 'clarify') {
      this._appendChatMessage(
        'assistant',
        payload.question || '请补充一些照片线索',
        payload.options || [],
      );
      this.setData({ awaitingClarification: true, agentProgress: '' });
      return;
    }
    if (event.type === 'final') {
      this._appendChatMessage('assistant', payload.message || '处理完成');
      this.setData({ awaitingClarification: false, agentProgress: '' });
      return;
    }
    if (event.type === 'done') {
      this.setData({
        agentSessionId: payload.session_id || this.data.agentSessionId,
        agentStatus: payload.status || '',
        selectedResultId: (payload.state && payload.state.confirmed_photo_id)
          || this.data.selectedResultId,
        agentProgress: '',
      });
      return;
    }
    if (event.type === 'error') this.setData({ agentProgress: '' });
  },

  _confirmAgentGeneration(result) {
    const confirmation = result.confirmation || {};
    wx.showModal({
      title: '确认开始生成',
      content: `将使用你选中的照片，预计费用 ¥${Number(confirmation.estimated_cost_yuan || 0).toFixed(2)}。确认后才会开始。`,
      confirmText: '确认生成',
      success: async (res) => {
        if (!res.confirm) return;
        try {
          await generations.confirm(
            result.generation_id,
            confirmation.confirmation_id,
          );
          wx.showToast({ title: '生成任务已提交', icon: 'success' });
        } catch (err) {
          wx.showToast({ title: err.detail || '提交失败，可稍后重试', icon: 'none' });
        }
      },
    });
  },

  _appendChatMessage(role, text, options = []) {
    messageSequence += 1;
    const id = `chat-message-${Date.now()}-${messageSequence}`;
    this.setData({
      chatMessages: this.data.chatMessages.concat([{ id, role, text, options }]),
      chatScrollTarget: id,
    });
  },

  _resolveThumb(url) {
    if (!url) return '';
    if (url.startsWith('http')) return url;
    return this.data.apiBase + url;
  },

  onPreviewResult(e) {
    const index = Number(e.currentTarget.dataset.index);
    const urls = this.data.results.map((item) => item.thumb_url_full).filter(Boolean);
    const current = this.data.results[index] && this.data.results[index].thumb_url_full;
    if (current) wx.previewImage({ current, urls });
  },

  onLoadMoreResults() {
    const nextCount = Math.min(
      this.data.results.length + RESULT_RENDER_BATCH,
      completeResultBuffer.length,
    );
    this.setData({
      results: completeResultBuffer.slice(0, nextCount),
      hasMoreResults: nextCount < completeResultBuffer.length,
    });
  },

  onSelectResult(e) {
    if (this.data.loading) return;
    const index = Number(e.currentTarget.dataset.index);
    const item = this.data.results[index];
    if (!item || !item.id) return;
    wx.showModal({
      title: `选择第 ${index + 1} 张？`,
      content: '确认后 Photo Agent 会把这张照片作为你本人选中的照片。',
      confirmText: '确认选择',
      success: (res) => {
        if (res.confirm) this._confirmResultSelection(item, index);
      },
    });
  },

  async _confirmResultSelection(item, index) {
    const sessionId = this.data.agentSessionId;
    if (!sessionId) {
      wx.showToast({ title: '会话已失效，请重新搜索', icon: 'none' });
      return;
    }
    const message = `我选择第 ${index + 1} 张`;
    this._appendChatMessage('user', message);
    this.setData({
      loading: true,
      selectedResultId: item.id,
      agentProgress: '正在确认你的选择…',
    });
    search.click({ photo_id: item.id, query: '', rank: index }).catch(() => {});
    try {
      await agent.stream({
        query: message,
        session_id: sessionId,
        selected_photo_id: item.id,
        onEvent: (event) => this._handleAgentEvent(event, this.data.resultMode),
      });
    } catch (err) {
      const detail = typeof err.detail === 'string' ? err.detail : '确认选择失败';
      this._appendChatMessage('assistant', detail);
      this.setData({ selectedResultId: '' });
      wx.showToast({ title: detail.slice(0, 20), icon: 'none' });
    } finally {
      this.setData({ loading: false, agentProgress: '' });
    }
  },

  // -------- 语音输入 --------
  onVoiceStart() {
    this.setData({ recording: true });
    recorder.start({
      duration: 30000,
      sampleRate: 16000,
      numberOfChannels: 1,
      encodeBitRate: 48000,
      format: 'mp3',
    });
    recorder.onStop((res) => {
      voicePath = res.tempFilePath;
      this.setData({ recording: false });
      wx.showModal({
        title: '语音已录制',
        content: '目前 MVP 版本还未接入 ASR 服务，请先手动输入。语音文件已保存到临时目录。',
        showCancel: false,
      });
    });
  },

  onVoiceEnd() {
    if (this.data.recording) recorder.stop();
  },
});
