// app.js —— 全局入口
const { API_BASE } = require('./utils/config');

App({
  globalData: {
    apiBase: API_BASE,
    user: null,        // { id, nickname, avatar_url }
    authEpoch: 0,
    authValidated: false,
    token: null,       // JWT
    lastTrace: null,   // { logId, traceId }，报障时用于服务端精确定位
  },

  onLaunch() {
    // 恢复本地存储的登录态
    try {
      const token = wx.getStorageSync('token');

      if (token) {
        this.globalData.token = token;
        const epoch = this.globalData.authEpoch;
        this.authReady = require('./utils/api').auth.me().then(user => {
          if (epoch === this.globalData.authEpoch && token === this.globalData.token) {
            this.globalData.user = user;
            this.globalData.authValidated = true;
          }
        }).catch(() => {});
      }

    } catch (e) {
      console.warn('read storage failed', e);
    }
  },

  isLoggedIn() {
    return !!this.globalData.token && this.globalData.authValidated;
  },

  setLogin({ token, user }) {
    this.globalData.authEpoch += 1;
    this.globalData.authValidated = true;
    this.globalData.token = token;
    this.globalData.user = user;
    wx.setStorageSync('token', token);
    wx.setStorageSync('user', user);
  },

  invalidateSession(token, epoch) {
    if (token && token === this.globalData.token && epoch === this.globalData.authEpoch) {
      this.logout();
      wx.reLaunch({ url: '/pages/login/index' });
    }
  },

  logout() {
    this.globalData.authEpoch += 1;
    this.globalData.authValidated = false;
    this.globalData.token = null;
    this.globalData.user = null;
    wx.removeStorageSync('token');
    wx.removeStorageSync('user');
  },
});
