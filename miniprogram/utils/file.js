// utils/file.js —— 文件操作：读大小、算 SHA-256、二进制 PUT
const fs = wx.getFileSystemManager();

/**
 * 拿到本地临时文件的字节大小。
 */
function fileSize(filePath) {
  return new Promise((resolve, reject) => {
    fs.getFileInfo({
      filePath,
      success: (r) => resolve(r.size),
      fail: (e) => reject(e),
    });
  });
}

/**
 * 计算本地文件 SHA-256。
 * 小程序原生 crypto 不能算 SHA-256，用 pure JS 实现（一次调用够快）。
 * 参考 tiny-sha256（MIT），下面是内联版本，避免额外依赖。
 */
function sha256Hex(buffer) {
  // 把 ArrayBuffer 转成 Uint8Array 后走 sha256
  const bytes = new Uint8Array(buffer);
  return _sha256Bytes(bytes);
}

async function fileSha256(filePath) {
  const buf = await new Promise((resolve, reject) => {
    fs.readFile({
      filePath,
      success: (r) => resolve(r.data),
      fail: (e) => reject(e),
    });
  });
  return sha256Hex(buf);
}

/**
 * PUT 直传到 OSS 或 mock 端点。
 */
function uploadPut(url, filePath, headers = {}) {
  return new Promise((resolve, reject) => {
    // 微信小程序 wx.uploadFile 是 multipart，签名 URL 场景应该走 wx.request + arrayBuffer
    fs.readFile({
      filePath,
      success(r) {
        wx.request({
          url,
          method: 'PUT',
          data: r.data,           // ArrayBuffer 会作为 body 原样发送
          header: headers,
          success: (res) => {
            if (res.statusCode >= 200 && res.statusCode < 300) resolve(res);
            else reject({ status: res.statusCode, detail: res.data });
          },
          fail: (e) => reject(e),
        });
      },
      fail: reject,
    });
  });
}

module.exports = { fileSize, fileSha256, uploadPut };

// ---------- 下方为 tiny SHA-256 pure JS 实现（MIT license） ----------

const _K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]);

function _rotr(x, n) { return (x >>> n) | (x << (32 - n)); }

function _sha256Bytes(bytes) {
  const l = bytes.length;
  const bitLen = l * 8;
  const withPad = new Uint8Array(((l + 9 + 63) >> 6) << 6);
  withPad.set(bytes);
  withPad[l] = 0x80;
  // 写 64bit 长度到末尾（小程序里长度 < 2^32，前 4 字节 0）
  new DataView(withPad.buffer).setUint32(withPad.length - 4, bitLen >>> 0, false);

  const H = new Uint32Array([
    0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
    0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19,
  ]);
  const W = new Uint32Array(64);
  const view = new DataView(withPad.buffer);

  for (let i = 0; i < withPad.length; i += 64) {
    for (let t = 0; t < 16; t++) W[t] = view.getUint32(i + t * 4, false);
    for (let t = 16; t < 64; t++) {
      const s0 = _rotr(W[t - 15], 7) ^ _rotr(W[t - 15], 18) ^ (W[t - 15] >>> 3);
      const s1 = _rotr(W[t - 2], 17) ^ _rotr(W[t - 2], 19) ^ (W[t - 2] >>> 10);
      W[t] = (W[t - 16] + s0 + W[t - 7] + s1) >>> 0;
    }
    let a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
    for (let t = 0; t < 64; t++) {
      const S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + _K[t] + W[t]) >>> 0;
      const S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22);
      const mj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + mj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
    H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
    H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  let hex = '';
  for (let i = 0; i < 8; i++) hex += H[i].toString(16).padStart(8, '0');
  return hex;
}
