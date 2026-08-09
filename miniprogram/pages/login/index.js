// pages/login/index.js
const { auth } = require('../../utils/api');

Page({
  data: {
    nickname: '',
    avatarUrl: '',
    loading: false,
  },

  onLoad() {
    // 已登录直接跳过
    const app = getApp();
    if (app.isLoggedIn()) {
      wx.switchTab({ url: '/pages/timeline/index' });
    }
  },

  onChooseAvatar(e) {
    this.setData({ avatarUrl: e.detail.avatarUrl });
  },

  onNicknameChange(e) {
    this.setData({ nickname: e.detail.value });
  },

  async onLogin() {
    if (this.data.loading) return;
    this.setData({ loading: true });
    try {
      // 1. 拿微信 code
      const { code } = await new Promise((resolve, reject) => {
        wx.login({ success: resolve, fail: reject });
      });
      // 2. 换 JWT
      const resp = await auth.wechatLogin({
        code,
        nickname: this.data.nickname || undefined,
        avatar_url: this.data.avatarUrl || undefined,
      });
      // 3. 拉自己的信息
      const app = getApp();
      app.globalData.token = resp.access_token;
      const me = await auth.me();
      app.setLogin({ token: resp.access_token, user: me });

      wx.showToast({ title: '登录成功', icon: 'success' });
      setTimeout(() => wx.switchTab({ url: '/pages/timeline/index' }), 500);
    } catch (err) {
      console.error('login failed', err);
      wx.showModal({
        title: '登录失败',
        content: (err && err.detail) || '请检查后端是否可访问',
        showCancel: false,
      });
    } finally {
      this.setData({ loading: false });
    }
  },
});
