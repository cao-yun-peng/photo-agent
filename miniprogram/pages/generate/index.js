// pages/generate/index.js
const { skills, photos: photosApi, generations, API_BASE } = require('../../utils/api');

Page({
  _pendingIdempotencyKey: '',

  data: {
    skillId: null,
    skill: null,
    photos: [],           // 用户全部照片，用于选源图
    selectedPhotoId: null,
    extraPrompt: '',
    genId: null,
    genStatus: null,      // pending / processing / done / failed
    genResultUrl: null,
    genError: null,
    submitting: false,
    apiBase: API_BASE,
  },

  onLoad(query) {
    this.setData({ skillId: query.skill_id });
    if (query.skill_id) {
      skills.detail(query.skill_id).then(s => this.setData({ skill: s }));
    }
    this.loadPhotos();
  },

  async loadPhotos() {
    try {
      const list = await photosApi.list({ limit: 60 });
      const items = (list || []).map(p => ({
        ...p,
        thumb_url_full: p.thumb_url && (p.thumb_url.startsWith('http') ? p.thumb_url : this.data.apiBase + p.thumb_url),
      }));
      this.setData({ photos: items });
    } catch (err) {
      wx.showToast({ title: err.detail || '拉取失败', icon: 'none' });
    }
  },

  onSelectPhoto(e) {
    this.setData({ selectedPhotoId: e.currentTarget.dataset.id });
  },

  onExtraInput(e) {
    this.setData({ extraPrompt: e.detail.value });
  },

  async onGenerate() {
    if (!this.data.selectedPhotoId) {
      wx.showToast({ title: '先选一张照片', icon: 'none' });
      return;
    }
    this.setData({ submitting: true, genStatus: null, genResultUrl: null });
    try {
      const g = await generations.create(this.data.selectedPhotoId, {
        skill_id: this.data.skillId || null,
        extra_prompt: this.data.extraPrompt || null,
        idempotency_key: this._pendingIdempotencyKey || (
          this._pendingIdempotencyKey = `wx-${Date.now()}-${Math.random().toString(16).slice(2)}`
        ),
      });
      this.setData({ genId: g.id, genStatus: g.status });
      if (g.status === 'awaiting_confirmation') {
        const accepted = await new Promise((resolve) => {
          wx.showModal({
            title: '确认开始生成',
            content: `将使用当前照片和效果，预计费用 ¥${Number(g.estimated_cost_yuan || 0).toFixed(2)}。确认后才会开始生成。`,
            confirmText: '确认生成',
            success: (res) => resolve(Boolean(res.confirm)),
            fail: () => resolve(false),
          });
        });
        if (!accepted) {
          this.setData({ submitting: false });
          return;
        }
        const confirmed = await generations.confirm(g.id, g.confirmation_token);
        this.setData({ genStatus: confirmed.status });
      }
      this._pendingIdempotencyKey = '';
      this._poll();
    } catch (err) {
      this.setData({ submitting: false });
      wx.showModal({
        title: err.status === 429 ? '今日额度用完' : '生成失败',
        content: err.detail || '未知错误',
        showCancel: false,
      });
    }
  },

  async _poll() {
    if (!this.data.genId) return;
    let waited = 0;
    const tick = async () => {
      try {
        const g = await generations.detail(this.data.genId);
        this.setData({ genStatus: g.status });
        if (g.status === 'done') {
          // mock 模式下 result_url 是相对路径，需要拼接 API_BASE
          const url = g.result_url && (g.result_url.startsWith('http') ? g.result_url : this.data.apiBase + g.result_url);
          this.setData({ genResultUrl: url, submitting: false });
          return;
        }
        if (g.status === 'failed') {
          this.setData({ genError: g.error_message, submitting: false });
          return;
        }
      } catch (e) { /* 忽略单次失败 */ }
      waited += 3;
      if (waited > 180) {
        this.setData({ submitting: false, genError: '超时（> 180s），稍后到"我的生成"查看' });
        return;
      }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  },

  onSave() {
    if (!this.data.genResultUrl) return;
    wx.downloadFile({
      url: this.data.genResultUrl,
      success: (r) => {
        wx.saveImageToPhotosAlbum({
          filePath: r.tempFilePath,
          success: () => wx.showToast({ title: '已保存', icon: 'success' }),
          fail: () => wx.showToast({ title: '未授权保存', icon: 'none' }),
        });
      },
    });
  },
});
