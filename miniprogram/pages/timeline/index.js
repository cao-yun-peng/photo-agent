// pages/timeline/index.js
const { photos, API_BASE } = require('../../utils/api');

Page({
  data: {
    items: [],
    loading: false,
    offset: 0,
    limit: 20,
    hasMore: true,
    apiBase: API_BASE,
  },

  onShow() {
    const app = getApp();
    if (!app.isLoggedIn()) {
      wx.reLaunch({ url: '/pages/login/index' });
      return;
    }
    // 每次进入自动刷新第一页
    this.refresh();
  },

  onPullDownRefresh() {
    this.refresh().finally(() => wx.stopPullDownRefresh());
  },

  onReachBottom() {
    if (this.data.hasMore && !this.data.loading) {
      this.loadMore();
    }
  },

  async refresh() {
    this.setData({ offset: 0, hasMore: true });
    return this._fetchAndAppend(true);
  },

  async loadMore() {
    return this._fetchAndAppend(false);
  },

  async _fetchAndAppend(reset) {
    if (this.data.loading) return;
    this.setData({ loading: true });
    try {
      const data = await photos.list({
        limit: this.data.limit,
        offset: reset ? 0 : this.data.offset,
      });
      const decorated = (data || []).map((p) => ({
        ...p,
        thumb_url_full: this._resolveThumb(p.thumb_url),
      }));
      this.setData({
        items: reset ? decorated : this.data.items.concat(decorated),
        offset: (reset ? 0 : this.data.offset) + decorated.length,
        hasMore: decorated.length === this.data.limit,
      });
    } catch (err) {
      wx.showToast({ title: (err.detail || '拉取失败').slice(0, 20), icon: 'none' });
    } finally {
      this.setData({ loading: false });
    }
  },

  _resolveThumb(url) {
    if (!url) return '';
    if (url.startsWith('http')) return url;
    return this.data.apiBase + url;
  },

  onTapItem(e) {
    const id = e.currentTarget.dataset.id;
    wx.showActionSheet({
      itemList: ['查看详情', 'AI 改造', '删除'],
      success: async (res) => {
        if (res.tapIndex === 0) {
          const detail = await photos.detail(id);
          wx.showModal({
            title: '照片详情',
            content: `描述：${detail.ai_description || '（还在处理）'}\n状态：${detail.status}`,
            showCancel: false,
          });
        } else if (res.tapIndex === 1) {
          wx.navigateTo({ url: `/pages/skills/index?source_photo_id=${id}` });
        } else if (res.tapIndex === 2) {
          wx.showModal({
            title: '确认删除？',
            content: '删除后不可恢复',
            success: async (r) => {
              if (r.confirm) {
                try {
                  await photos.remove(id);
                  wx.showToast({ title: '已删除', icon: 'success' });
                  this.refresh();
                } catch (err) {
                  wx.showToast({ title: err.detail || '删除失败', icon: 'none' });
                }
              }
            },
          });
        }
      },
    });
  },

  onTapUpload() {
    wx.switchTab({ url: '/pages/upload/index' });
  },
});
