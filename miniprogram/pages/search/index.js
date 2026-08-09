// pages/search/index.js
const { search, API_BASE } = require('../../utils/api');

const recorder = wx.getRecorderManager();
let voicePath = null;

Page({
  data: {
    q: '',
    quickChips: ['最近一周', '上个月', '美食', '风景', '人像'],
    activeChip: '',
    results: [],
    parsed: null,
    loading: false,
    cacheHit: false,
    nextCursor: null,
    recording: false,
    apiBase: API_BASE,
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
    this.setData({ q: chip, activeChip: chip }, () => this.onSearch(true));
  },

  async onSearch(autoParse) {
    const q = (this.data.q || '').trim();
    if (!q) {
      wx.showToast({ title: '请输入或说一句话', icon: 'none' });
      return;
    }
    this.setData({ loading: true, results: [], nextCursor: null });
    try {
      const resp = await search.query({
        q,
        limit: 20,
        auto_parse: !!autoParse,
      });
      this.setData({
        results: (resp.items || []).map((it) => ({
          ...it,
          thumb_url_full: this._resolveThumb(it.thumb_url),
        })),
        parsed: resp.parsed,
        cacheHit: !!resp.cache_hit,
        nextCursor: resp.next_cursor,
      });
      if (resp.items.length === 0) {
        wx.showToast({ title: '没有找到相关照片', icon: 'none' });
      }
    } catch (err) {
      wx.showToast({ title: (err.detail || '搜索失败').slice(0, 20), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  async onLoadMore() {
    if (!this.data.nextCursor || this.data.loading) return;
    this.setData({ loading: true });
    try {
      const resp = await search.query({
        q: this.data.q,
        limit: 20,
        cursor: this.data.nextCursor,
      });
      this.setData({
        results: this.data.results.concat(
          (resp.items || []).map((it) => ({
            ...it,
            thumb_url_full: this._resolveThumb(it.thumb_url),
          })),
        ),
        nextCursor: resp.next_cursor,
      });
    } catch (err) {
      wx.showToast({ title: '加载更多失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  _resolveThumb(url) {
    if (!url) return '';
    if (url.startsWith('http')) return url;
    return this.data.apiBase + url;
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
      // 微信小程序有内置的插件"同声传译"能做 ASR，但需要单独申请。
      // MVP 阶段：录音后弹一个提示让用户手动输入，或者接你自己的 ASR。
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
