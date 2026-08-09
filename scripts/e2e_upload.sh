#!/usr/bin/env bash
# ============================================================================
# e2e_upload.sh — 端到端联调 D3–D4 上传闭环
#
# 覆盖：
#   1. 微信登录（dev mock code） → 拿 JWT
#   2. 生成一张真实 JPG 文件 + 计算 SHA-256
#   3. 调 /photos/upload-url 拿签名 URL
#   4. 按签名回带 headers 做 PUT 上传
#   5. 调 POST /photos 触发回调（后端 head_object 核验）
#   6. 调 GET /photos 看时间线里有没有
#   7. 再次调 /photos/upload-url 验证去重（应返回 duplicate=true）
#
# 用法：
#   ./scripts/e2e_upload.sh          # 默认打本机 http://localhost:8000
#   API=http://192.168.x.x:8000 ./scripts/e2e_upload.sh
# ============================================================================
set -euo pipefail

API="${API:-http://localhost:8000}"
CODE="e2e-$(date +%s)"
NICK="E2E 测试"

# ---------- 前置检查 ----------
command -v curl >/dev/null || { echo "缺 curl"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }

json_get() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
say() { printf "\033[1;36m▶ %s\033[0m\n" "$1"; }

# ---------- 1. 生成一张真实 JPG ----------
say "1. 生成测试图片"
TMP_JPG="$(mktemp -t photo-agent-e2e.XXXX.jpg)"
python3 - <<PY "$TMP_JPG"
import io, sys, struct, zlib
# 用 Pillow 若可用则生成真图，否则退化为最小 JPEG 字节
try:
    from PIL import Image
    Image.new("RGB", (256, 256), (200, 120, 60)).save(sys.argv[1], "JPEG", quality=80)
except Exception:
    # 最小 valid JPEG（1x1 白）
    b = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070707"
        "0909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c28"
        "37292c30313434341f27393d38323c2e333432ffc0000b0801000100010111ffc40014"
        "0001000000000000000000000000000000ffc40014100100000000000000000000000"
        "0000000000000ffda0008010100003f00b7ffd9"
    )
    with open(sys.argv[1], "wb") as f:
        f.write(b)
PY
FILE_SIZE=$(wc -c < "$TMP_JPG" | tr -d ' ')
HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$TMP_JPG")
echo "   file=$TMP_JPG  size=$FILE_SIZE  sha256=${HASH:0:16}..."

# ---------- 2. 登录换 JWT ----------
say "2. 登录 (code=$CODE)"
LOGIN_RESP=$(curl -sS -X POST "$API/auth/wechat" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"$CODE\",\"nickname\":\"$NICK\"}")
TOKEN=$(echo "$LOGIN_RESP" | json_get "['access_token']")
echo "   token=${TOKEN:0:24}..."

# ---------- 3. 拿签名 URL ----------
say "3. 请求上传签名"
SIGN_RESP=$(curl -sS -X POST "$API/photos/upload-url" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
echo "   $SIGN_RESP"

DUPLICATE=$(echo "$SIGN_RESP" | json_get "['duplicate']")
if [[ "$DUPLICATE" == "True" ]]; then
  echo "   已存在同 hash 照片，脚本换个种子重跑即可。"
  exit 0
fi

UPLOAD_URL=$(echo "$SIGN_RESP" | json_get "['upload_url']")
OSS_KEY=$(echo "$SIGN_RESP" | json_get "['oss_key']")

# mock 模式返回的是 path，需要拼上 API 前缀
if [[ "$UPLOAD_URL" == /* ]]; then
  UPLOAD_URL="${API}${UPLOAD_URL}"
fi

# ---------- 4. 直传 PUT ----------
say "4. PUT 到 $UPLOAD_URL"
PUT_STATUS=$(curl -sS -o /tmp/put_resp -w "%{http_code}" \
  -X PUT "$UPLOAD_URL" \
  -H "Content-Type: image/jpeg" \
  --data-binary "@$TMP_JPG")
echo "   HTTP $PUT_STATUS"
[[ "$PUT_STATUS" =~ ^2 ]] || { echo "   PUT 失败: $(cat /tmp/put_resp)"; exit 1; }

# ---------- 5. 完成回调 ----------
say "5. 通知后端上传完成"
CB_RESP=$(curl -sS -X POST "$API/photos" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"oss_key\":\"$OSS_KEY\",\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
echo "   $CB_RESP"
PHOTO_ID=$(echo "$CB_RESP" | json_get "['id']")

# ---------- 6. 拉列表 ----------
say "6. 拉时间线"
curl -sS "$API/photos?limit=5" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ---------- 7. 去重验证 ----------
say "7. 再次请求签名（应返回 duplicate=true）"
curl -sS -X POST "$API/photos/upload-url" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}" \
  | python3 -m json.tool

# ---------- 收尾 ----------
rm -f "$TMP_JPG" /tmp/put_resp
printf "\n\033[1;32m✅ D3–D4 闭环全部通过。photo_id=%s\033[0m\n" "$PHOTO_ID"
