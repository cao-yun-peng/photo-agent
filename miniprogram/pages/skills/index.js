// pages/skills/index.js
const { skills, API_BASE } = require('../../utils/api');

Page({
  data: {
    tab: 'plaza',    // 'plaza' or 'mine'
    items: [],
    loading: false,
    quota: null,
  },

  onShow() {
    const app = getApp();
    if (!app.isLoggedIn()) {
      wx.reLaunch({ url: '/pages/login/index' });
      return;
    }
    this.loadQuota();
    this.load();
  },

  async loadQuota() {
    try {
      const q = await skills.quota();
      this.setData({ quota: q });
    } catch (e) { /* ignore */ }
  },

  async load() {
    this.setData({ loading: true });
    try {
      const data = this.data.tab === 'plaza'
        ? await skills.plaza({ limit: 50 })
        : await skills.mine();
      // mock 模式下 cover_url 是相对路径，需要拼接 API_BASE
      const items = data.map(s => ({
        ...s,
        cover_url: this._resolveUrl(s.cover_url),
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

  onSwitchTab(e) {
    const t = e.currentTarget.dataset.tab;
    this.setData({ tab: t, items: [] }, () => this.load());
  },

  onCreate() {
    wx.navigateTo({ url: '/pages/skill-edit/index' });
  },

  onTapSkill(e) {
    const id = e.currentTarget.dataset.id;
    const item = this.data.items.find(s => s.id === id);
    if (!item) return;
    wx.showActionSheet({
      itemList: item.is_official || item.owner_id !== getApp().globalData.user.id
        ? ['查看提示词', '用它改造一张照片']
        : ['查看提示词', '用它改造一张照片', '编辑', '删除'],
      success: async (r) => {
        if (r.tapIndex === 0) {
          wx.showModal({
            title: item.name,
            content: item.prompt_template,
            showCancel: false,
          });
        } else if (r.tapIndex === 1) {
          wx.navigateTo({ url: `/pages/generate/index?skill_id=${item.id}` });
        } else if (r.tapIndex === 2) {
          wx.navigateTo({ url: `/pages/skill-edit/index?id=${item.id}` });
        } else if (r.tapIndex === 3) {
          const c = await new Promise(res => wx.showModal({ title: '删除？', success: res }));
          if (c.confirm) {
            await skills.remove(item.id);
            this.load();
          }
        }
      },
    });
  },

  onViewHistory() {
    wx.navigateTo({ url: '/pages/generations/index' });
  },
});
