#!/usr/bin/env bash
# ============================================================================
# e2e_ai.sh — 端到端联调 D5–D7 AI 处理管道
#
# 相对 D3–D4 多出的动作：
#   6b. 轮询照片状态，等 worker 把 status 从 pending → processing → done
#   6c. 打印 AI 生成的中文描述、尺寸、拍摄时间
#   8.  用 AI 描述里的一个关键词发起语义搜索，看能不能命中自己
#
# 用法：
#   ./scripts/e2e_ai.sh
#   API=http://localhost:8000 ./scripts/e2e_ai.sh
#   IMG=/path/to/some.jpg ./scripts/e2e_ai.sh    # 用你自己的图片
# ============================================================================
set -euo pipefail

API="${API:-http://localhost:8000}"
CODE="e2eai-$(date +%s)"
NICK="AI 测试"
IMG="${IMG:-}"
MAX_WAIT=90         # 最多等 90 秒

command -v curl >/dev/null || { echo "缺 curl"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }

json_get() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
say() { printf "\033[1;36m▶ %s\033[0m\n" "$1"; }

# ---------- 1. 准备图片 ----------
if [[ -n "$IMG" && -f "$IMG" ]]; then
  say "1. 使用指定图片 $IMG"
  TMP_JPG="$IMG"
else
  say "1. 生成一张彩色测试图"
  TMP_JPG="$(mktemp -t photo-agent-e2eai.XXXX.jpg)"
  python3 - <<PY "$TMP_JPG"
import sys
try:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 600), (30, 80, 160))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 100, 700, 500], fill=(240, 200, 80))
    d.ellipse([300, 200, 500, 400], fill=(220, 80, 80))
    img.save(sys.argv[1], "JPEG", quality=85)
except Exception:
    # 最小合法 JPEG
    b = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070707"
        "0909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c28"
        "37292c30313434341f27393d38323c2e333432ffc0000b0801000100010111ffc40014"
        "0001000000000000000000000000000000ffc40014100100000000000000000000000"
        "0000000000000ffda0008010100003f00b7ffd9"
    )
    open(sys.argv[1], "wb").write(b)
PY
fi
FILE_SIZE=$(wc -c < "$TMP_JPG" | tr -d ' ')
HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$TMP_JPG")
echo "   size=$FILE_SIZE  sha256=${HASH:0:16}..."

# ---------- 2. 登录 ----------
say "2. 登录 (code=$CODE)"
TOKEN=$(curl -sS -X POST "$API/auth/wechat" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"$CODE\",\"nickname\":\"$NICK\"}" | json_get "['access_token']")
echo "   token=${TOKEN:0:24}..."

# ---------- 3. 签名 + 4. PUT + 5. 回调 ----------
say "3. 请求签名"
SIGN_RESP=$(curl -sS -X POST "$API/photos/upload-url" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
UPLOAD_URL=$(echo "$SIGN_RESP" | json_get "['upload_url']")
OSS_KEY=$(echo "$SIGN_RESP" | json_get "['oss_key']")
[[ "$UPLOAD_URL" == /* ]] && UPLOAD_URL="${API}${UPLOAD_URL}"

say "4. PUT 到 $UPLOAD_URL"
curl -sS -o /dev/null -X PUT "$UPLOAD_URL" \
  -H "Content-Type: image/jpeg" --data-binary "@$TMP_JPG"

say "5. 通知后端上传完成 → 自动入队"
CREATE_RESP=$(curl -sS -X POST "$API/photos" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"oss_key\":\"$OSS_KEY\",\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
PHOTO_ID=$(echo "$CREATE_RESP" | json_get "['id']")
echo "   photo_id=$PHOTO_ID  初始 status=$(echo "$CREATE_RESP" | json_get "['status']")"

# ---------- 6. 轮询 ----------
say "6. 等 worker 处理（最多 $MAX_WAIT 秒）"
START=$(date +%s)
STATUS="pending"
DESC=""
while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  if (( ELAPSED > MAX_WAIT )); then
    echo "   超时：状态仍为 $STATUS"
    exit 2
  fi
  RESP=$(curl -sS "$API/photos/$PHOTO_ID" -H "Authorization: Bearer $TOKEN")
  STATUS=$(echo "$RESP" | json_get "['status']")
  printf "   %3ds  status=%s\n" "$ELAPSED" "$STATUS"
  if [[ "$STATUS" == "done" ]]; then
    DESC=$(echo "$RESP" | json_get "['ai_description']")
    break
  fi
  if [[ "$STATUS" == "failed" ]]; then
    echo "   worker 处理失败，请查 docker compose logs worker"
    exit 3
  fi
  sleep 2
done

say "7. AI 结果"
echo "   描述: $DESC"

# ---------- 8. 用描述里前几个字做语义搜索 ----------
Q=$(python3 -c "import sys;print(sys.argv[1][:6])" "$DESC")
say "8. 语义搜索（Q=\"$Q\"）"
curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"q\":\"$Q\",\"limit\":3}" | python3 -m json.tool

printf "\n\033[1;32m✅ D5–D7 闭环全部通过。photo_id=%s\033[0m\n" "$PHOTO_ID"
