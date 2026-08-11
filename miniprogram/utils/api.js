// utils/api.js —— HTTP 封装 + JWT 自动加头
const { API_BASE } = require('./config');

function _getToken() {
  const app = getApp();
  return app && app.globalData && app.globalData.token;
}

/**
 * 统一请求封装。用法：
 *   const data = await request({ url: '/photos', method: 'GET' });
 *
 * 未提供 auth=false 时会自动带上 Authorization。
 * 出错时抛出 { status, detail }。
 */
function request({ url, method = 'GET', data, header = {}, auth = true, timeout = 30000 }) {
  return new Promise((resolve, reject) => {
    const h = { 'Content-Type': 'application/json', ...header };
    if (auth) {
      const token = _getToken();
      if (token) h['Authorization'] = 'Bearer ' + token;
    }
    wx.request({
      url: API_BASE + url,
      method,
      data,
      header: h,
      timeout,
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else {
          const detail = (res.data && (res.data.errMsg || res.data.detail || res.data.message)) || res.errMsg;
          reject({ status: res.statusCode, detail });
        }
      },
      fail(err) {
        reject({ status: 0, detail: err.errMsg || String(err) });
      },
    });
  });
}

// -------- 具体 API 便利函数 --------
const auth = {
  wechatLogin({ code, nickname, avatar_url }) {
    return request({ url: '/auth/wechat', method: 'POST', data: { code, nickname, avatar_url }, auth: false });
  },
  me() {
    return request({ url: '/auth/me' });
  },
};

const photos = {
  list({ limit = 20, offset = 0 } = {}) {
    return request({ url: `/photos?limit=${limit}&offset=${offset}` });
  },
  detail(id) {
    return request({ url: `/photos/${id}` });
  },
  requestUploadUrl({ hash, size_bytes, mime_type = 'image/jpeg' }) {
    return request({
      url: '/photos/upload-url',
      method: 'POST',
      data: { hash, size_bytes, mime_type },
    });
  },
  finishUpload({ oss_key, hash, size_bytes, mime_type = 'image/jpeg' }) {
    return request({
      url: '/photos',
      method: 'POST',
      data: { oss_key, hash, size_bytes, mime_type },
    });
  },
  remove(id) {
    return request({ url: `/photos/${id}`, method: 'DELETE' });
  },
};

const search = {
  query(payload) {
    return request({ url: '/search', method: 'POST', data: payload });
  },
};

const skills = {
  mine() {
    return request({ url: '/skills' });
  },
  plaza({ limit = 30, offset = 0 } = {}) {
    return request({ url: `/skills/plaza?limit=${limit}&offset=${offset}` });
  },
  detail(id) {
    return request({ url: `/skills/${id}` });
  },
  create(payload) {
    return request({ url: '/skills', method: 'POST', data: payload });
  },
  update(id, payload) {
    return request({ url: `/skills/${id}`, method: 'PATCH', data: payload });
  },
  remove(id) {
    return request({ url: `/skills/${id}`, method: 'DELETE' });
  },
  quota() {
    return request({ url: '/skills/_/quota' });
  },
};

const generations = {
  create(photoId, payload) {
    return request({
      url: `/photos/${photoId}/generate`,
      method: 'POST',
      data: payload,
    });
  },
  list({ limit = 20, offset = 0 } = {}) {
    return request({ url: `/generations?limit=${limit}&offset=${offset}` });
  },
  detail(id) {
    return request({ url: `/generations/${id}` });
  },
};

module.exports = { request, auth, photos, search, skills, generations, API_BASE };
