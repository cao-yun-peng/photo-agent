// utils/api.js —— HTTP 封装 + JWT 自动加头
const { API_BASE } = require('./config');

function _getToken() {
  const app = getApp();
  return app && app.globalData && app.globalData.token;
}

function _newLogId() {
  return 'wx-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
}

function _captureTraceHeaders(headers = {}) {
  const normalized = {};
  Object.keys(headers).forEach((key) => { normalized[key.toLowerCase()] = headers[key]; });
  const trace = {
    logId: normalized['x-log-id'] || null,
    traceId: normalized['x-trace-id'] || null,
  };
  const app = getApp();
  if (app && app.globalData) app.globalData.lastTrace = trace;
  return trace;
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
    const h = { 'Content-Type': 'application/json', 'X-Log-ID': _newLogId(), ...header };
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
        const trace = _captureTraceHeaders(res.header);
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
        } else {
          const detail = (res.data && (res.data.errMsg || res.data.detail || res.data.message)) || res.errMsg;
          reject({ status: res.statusCode, detail, ...trace });
        }
      },
      fail(err) {
        reject({ status: 0, detail: err.errMsg || String(err), logId: h['X-Log-ID'] });
      },
    });
  });
}

// wx.request 的分块可能截断一个 UTF-8 中文字符；流式解码器会把残缺字节留到下一块。
function _createUtf8StreamDecoder() {
  let pending = new Uint8Array(0);
  return {
    decode(arrayBuffer, flush = false) {
      const incoming = arrayBuffer ? new Uint8Array(arrayBuffer) : new Uint8Array(0);
      const bytes = new Uint8Array(pending.length + incoming.length);
      bytes.set(pending, 0);
      bytes.set(incoming, pending.length);
      let text = '';
      let index = 0;

      while (index < bytes.length) {
        const first = bytes[index];
        let width = 1;
        let codePoint = first;
        if (first >= 0xc2 && first <= 0xdf) {
          width = 2;
          codePoint = first & 0x1f;
        } else if (first >= 0xe0 && first <= 0xef) {
          width = 3;
          codePoint = first & 0x0f;
        } else if (first >= 0xf0 && first <= 0xf4) {
          width = 4;
          codePoint = first & 0x07;
        } else if (first >= 0x80) {
          text += '\ufffd';
          index += 1;
          continue;
        }
        if (index + width > bytes.length) break;

        let valid = true;
        for (let offset = 1; offset < width; offset += 1) {
          const next = bytes[index + offset];
          if ((next & 0xc0) !== 0x80) {
            valid = false;
            break;
          }
          codePoint = (codePoint << 6) | (next & 0x3f);
        }
        if (!valid) {
          text += '\ufffd';
          index += 1;
          continue;
        }
        if (codePoint <= 0xffff) {
          text += String.fromCharCode(codePoint);
        } else {
          const value = codePoint - 0x10000;
          text += String.fromCharCode(
            0xd800 + (value >> 10),
            0xdc00 + (value & 0x3ff),
          );
        }
        index += width;
      }

      pending = bytes.slice(index);
      if (flush && pending.length) {
        text += '\ufffd';
        pending = new Uint8Array(0);
      }
      return text;
    },
  };
}

function _createSseParser(onEvent) {
  let buffer = '';

  function parseFrame(frame) {
    const data = frame
      .replace(/\r/g, '')
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n');
    if (data) onEvent(JSON.parse(data));
  }

  return {
    feed(text) {
      buffer += text;
      let separator = buffer.indexOf('\n\n');
      while (separator >= 0) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        if (frame.trim()) parseFrame(frame);
        separator = buffer.indexOf('\n\n');
      }
    },
    flush() {
      if (buffer.trim()) parseFrame(buffer);
      buffer = '';
    },
  };
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
  click(payload) {
    return request({ url: '/search/click', method: 'POST', data: payload });
  },
};

const agent = {
  stream({ query, session_id, selected_photo_id, onEvent }) {
    return new Promise((resolve, reject) => {
      const token = _getToken();
      const decoder = _createUtf8StreamDecoder();
      const events = [];
      let receivedChunks = false;
      let streamError = null;
      let settled = false;
      const logId = _newLogId();
      const parser = _createSseParser((event) => {
        events.push(event);
        if (event.type === 'error') streamError = event.payload || {};
        if (onEvent) onEvent(event);
      });

      const rejectOnce = (error) => {
        if (!settled) {
          settled = true;
          reject(error);
        }
      };
      const resolveOnce = () => {
        if (!settled) {
          settled = true;
          resolve(events);
        }
      };

      const task = wx.request({
        url: API_BASE + '/agent/stream',
        method: 'POST',
        data: {
          query,
          ...(session_id ? { session_id } : {}),
          ...(selected_photo_id ? { selected_photo_id } : {}),
        },
        header: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          'X-Log-ID': logId,
          ...(token ? { Authorization: 'Bearer ' + token } : {}),
        },
        enableChunked: true,
        timeout: 90000,
        success(res) {
          const trace = _captureTraceHeaders(res.header);
          try {
            if (!receivedChunks && typeof res.data === 'string') parser.feed(res.data);
            parser.feed(decoder.decode(null, true));
            parser.flush();
          } catch (error) {
            rejectOnce({ status: res.statusCode, detail: 'Agent 流数据解析失败：' + error.message, ...trace });
            return;
          }
          if (res.statusCode < 200 || res.statusCode >= 300) {
            const detail = (res.data && (res.data.detail || res.data.message)) || res.errMsg;
            rejectOnce({ status: res.statusCode, detail, ...trace });
            return;
          }
          if (streamError) {
            const rawDetail = streamError.detail || streamError.message || streamError.error;
            const detail = typeof rawDetail === 'string'
              ? rawDetail
              : (rawDetail && (rawDetail.message || rawDetail.error)) || 'Agent 执行失败';
            rejectOnce({ status: streamError.status_code || 0, detail, ...trace });
            return;
          }
          resolveOnce();
        },
        fail(err) {
          rejectOnce({ status: 0, detail: err.errMsg || String(err), logId });
        },
      });

      task.onChunkReceived((res) => {
        try {
          receivedChunks = true;
          parser.feed(decoder.decode(res.data));
        } catch (error) {
          task.abort();
          rejectOnce({ status: 0, detail: 'Agent 流数据解析失败：' + error.message });
        }
      });
    });
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
  confirm(id, confirmationToken) {
    return request({
      url: `/generations/${id}/confirm`,
      method: 'POST',
      data: { confirmation_token: confirmationToken },
    });
  },
};

module.exports = { request, auth, photos, search, agent, skills, generations, API_BASE };
