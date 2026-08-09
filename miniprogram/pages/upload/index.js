// pages/upload/index.js
const { photos, API_BASE } = require('../../utils/api');
const { fileSize, fileSha256, uploadPut } = require('../../utils/file');

Page({
  data: {
    tasks: [],       // [{ tmpPath, size, hash?, status, message }]
    uploading: false,
  },

  onShow() {
    const app = getApp();
    if (!app.isLoggedIn()) {
      wx.reLaunch({ url: '/pages/login/index' });
    }
  },

  async onChooseMedia() {
    try {
      const res = await new Promise((resolve, reject) => {
        wx.chooseMedia({
          count: 9,                 // 微信硬限制：单次最多 9 张
          mediaType: ['image'],
          sourceType: ['album', 'camera'],
          sizeType: ['original'],
          success: resolve,
          fail: reject,
        });
      });
      const tasks = res.tempFiles.map((f) => ({
        tmpPath: f.tempFilePath,
        size: f.size,
        status: 'queued',
        message: '待上传',
      }));
      this.setData({ tasks });
    } catch (err) {
      console.log('cancel choose', err);
    }
  },

  async onStartUpload() {
    if (this.data.uploading || this.data.tasks.length === 0) return;
    this.setData({ uploading: true });

    for (let i = 0; i < this.data.tasks.length; i++) {
      await this._uploadOne(i);
    }

    this.setData({ uploading: false });
    wx.showToast({ title: '批量完成', icon: 'success' });
  },

  async _uploadOne(index) {
    const update = (patch) => {
      const key = `tasks[${index}]`;
      const cur = this.data.tasks[index];
      this.setData({ [key]: { ...cur, ...patch } });
    };

    const t = this.data.tasks[index];
    try {
      update({ status: 'hashing', message: '正在计算指纹...' });
      const hash = await fileSha256(t.tmpPath);
      const size = t.size || (await fileSize(t.tmpPath));

      update({ status: 'signing', message: '请求上传签名...', hash });
      const sign = await photos.requestUploadUrl({
        hash, size_bytes: size, mime_type: 'image/jpeg',
      });

      if (sign.duplicate) {
        update({ status: 'done', message: '已存在，跳过' });
        return;
      }

      let uploadUrl = sign.upload_url;
      if (uploadUrl.startsWith('/')) uploadUrl = API_BASE + uploadUrl;

      update({ status: 'uploading', message: '上传中...' });
      await uploadPut(uploadUrl, t.tmpPath, sign.headers || { 'Content-Type': 'image/jpeg' });

      update({ status: 'finishing', message: '通知后端...' });
      await photos.finishUpload({
        oss_key: sign.oss_key,
        hash,
        size_bytes: size,
        mime_type: 'image/jpeg',
      });

      update({ status: 'done', message: '完成' });
    } catch (err) {
      console.error('upload one failed', err);
      update({ status: 'failed', message: (err && err.detail) || '失败' });
    }
  },

  onClearDone() {
    const tasks = this.data.tasks.filter((t) => t.status !== 'done');
    this.setData({ tasks });
  },

  onViewTimeline() {
    wx.switchTab({ url: '/pages/timeline/index' });
  },
});
