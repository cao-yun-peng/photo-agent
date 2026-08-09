// pages/skill-edit/index.js
const { skills, photos: photosApi, API_BASE } = require('../../utils/api');
const { fileSize, fileSha256, uploadPut } = require('../../utils/file');

Page({
  data: {
    id: null,
    name: '',
    description: '',
    prompt_template: '',
    reference_keys: [],
    reference_urls: [],   // 展示用
    is_public: false,
    model: 'wanx-v1',
    saving: false,
  },

  onLoad(query) {
    if (query.id) {
      this.setData({ id: query.id });
      this.loadSkill(query.id);
    }
  },

  async loadSkill(id) {
    try {
      const s = await skills.detail(id);
      this.setData({
        name: s.name,
        description: s.description || '',
        prompt_template: s.prompt_template,
        reference_keys: s.reference_keys || [],
        reference_urls: (s.reference_keys || []).map(k =>
          k.startsWith('http') ? k : `${API_BASE}/_mock/oss/${k}`
        ),
        is_public: s.is_public,
        model: s.model,
      });
    } catch (err) {
      wx.showToast({ title: err.detail || '加载失败', icon: 'none' });
    }
  },

  onNameInput(e) { this.setData({ name: e.detail.value }); },
  onDescInput(e) { this.setData({ description: e.detail.value }); },
  onPromptInput(e) { this.setData({ prompt_template: e.detail.value }); },
  onPublicChange(e) { this.setData({ is_public: e.detail.value }); },
  onModelChange(e) { this.setData({ model: e.detail.value }); },

  async onAddRef() {
    if (this.data.reference_keys.length >= 5) {
      wx.showToast({ title: '最多 5 张参考图', icon: 'none' });
      return;
    }
    try {
      const res = await new Promise((resolve, reject) =>
        wx.chooseMedia({ count: 1, mediaType: ['image'], success: resolve, fail: reject })
      );
      const f = res.tempFiles[0];
      wx.showLoading({ title: '上传参考图...', mask: true });
      const hash = await fileSha256(f.tempFilePath);
      const size = f.size || await fileSize(f.tempFilePath);
      let sign;
      try {
        sign = await photosApi.requestUploadUrl({ hash, size_bytes: size });
      } catch (err) {
        if (err.status === 409) {
          wx.hideLoading();
          wx.showToast({ title: '参考图已存在库中', icon: 'none' });
          return;
        }
        throw err;
      }
      let url = sign.upload_url;
      if (url.startsWith('/')) url = API_BASE + url;
      await uploadPut(url, f.tempFilePath, sign.headers);
      wx.hideLoading();
      this.setData({
        reference_keys: this.data.reference_keys.concat([sign.oss_key]),
        reference_urls: this.data.reference_urls.concat([f.tempFilePath]),
      });
    } catch (err) {
      wx.hideLoading();
      wx.showToast({ title: '取消或失败', icon: 'none' });
    }
  },

  onRemoveRef(e) {
    const i = e.currentTarget.dataset.index;
    const keys = [...this.data.reference_keys];
    const urls = [...this.data.reference_urls];
    keys.splice(i, 1); urls.splice(i, 1);
    this.setData({ reference_keys: keys, reference_urls: urls });
  },

  async onSave() {
    if (!this.data.name.trim() || !this.data.prompt_template.trim()) {
      wx.showToast({ title: '名字和提示词必填', icon: 'none' });
      return;
    }
    this.setData({ saving: true });
    try {
      const payload = {
        name: this.data.name.trim(),
        description: this.data.description.trim() || null,
        prompt_template: this.data.prompt_template.trim(),
        reference_keys: this.data.reference_keys,
        is_public: this.data.is_public,
        model: this.data.model,
      };
      if (this.data.id) {
        await skills.update(this.data.id, payload);
      } else {
        await skills.create(payload);
      }
      wx.showToast({ title: '已保存', icon: 'success' });
      setTimeout(() => wx.navigateBack(), 500);
    } catch (err) {
      wx.showToast({ title: err.detail || '保存失败', icon: 'none' });
    } finally {
      this.setData({ saving: false });
    }
  },
});
