// app.js —— 全局入口
const { API_BASE } = require('./utils/config');

App({
  globalData: {
    apiBase: API_BASE,
    user: null,        // { id, nickname, avatar_url }
    token: null,       // JWT
    lastTrace: null,   // { logId, traceId }，报障时用于服务端精确定位
  },

  onLaunch() {
    // 恢复本地存储的登录态
    try {
      const token = wx.getStorageSync('token');
      const user = wx.getStorageSync('user');
      if (token) this.globalData.token = token;
      if (user) this.globalData.user = user;
    } catch (e) {
      console.warn('read storage failed', e);
    }
  },

  isLoggedIn() {
    return !!this.globalData.token;
  },

  setLogin({ token, user }) {
    this.globalData.token = token;
    this.globalData.user = user;
    wx.setStorageSync('token', token);
    wx.setStorageSync('user', user);
  },

  logout() {
    this.globalData.token = null;
    this.globalData.user = null;
    wx.removeStorageSync('token');
    wx.removeStorageSync('user');
  },
});
