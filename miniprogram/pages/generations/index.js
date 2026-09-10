// pages/generations/index.js
const { generations, API_BASE } = require('../../utils/api');

Page({
  data: { items: [], loading: false },

  async onShow() {
    const app = getApp();
    await app.authReady;
    if (this.authEpoch !== app.globalData.authEpoch) {
      this.authEpoch = app.globalData.authEpoch;
      this.setData({ items: [], loading: false });
    }
    if (!app.isLoggedIn()) { wx.reLaunch({ url: '/pages/login/index' }); return; }
    this.load();
  },

  async load() {
    this.setData({ loading: true });
    try {
      const list = await generations.list({ limit: 50 });
      // mock 模式下 result_url 是相对路径，需要拼接 API_BASE
      const items = list.map(g => ({
        ...g,
        result_url: this._resolveUrl(g.result_url),
      }));
      this.setData({ items });
    } catch (err) {
      wx.showToast({ title: err.detail || '加载失败', icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  _resolveUrl(url) {
    if (!url) return '';
    if (url.startsWith('http')) return url;
    return API_BASE + url;
  },

  onPreview(e) {
    const url = e.currentTarget.dataset.url;
    if (!url) return;
    wx.previewImage({ current: url, urls: [url] });
  },
});
