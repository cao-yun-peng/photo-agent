#!/usr/bin/env bash
# ============================================================================
# e2e_generate.sh — 端到端联调 D15–D17 AI 改造
# 步骤：
#   1. 登录
#   2. 上传一张测试图（先跑 D3–D4 上传闭环）
#   3. 等 worker 处理完描述（AI 处理只是让 status=done，不生图）
#   4. 拉官方 Skill 列表 → 选第一个
#   5. POST /photos/{id}/generate 发起生成
#   6. 轮询 GET /generations/{id} 直到 status=done
#   7. 显示结果 URL
# ============================================================================
set -euo pipefail

API="${API:-http://localhost:8000}"
CODE="e2egen-$(date +%s)"
MAX_WAIT=180

command -v curl >/dev/null || { echo "缺 curl"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }

json_get() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }

# ---------- 1. 生成测试图 ----------
say "1. 生成测试图"
TMP_JPG="$(mktemp -t e2egen.XXXX.jpg)"
python3 - <<PY "$TMP_JPG"
import sys
try:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (768, 768), (60, 120, 180))
    d = ImageDraw.Draw(img)
    d.ellipse([200, 200, 568, 568], fill=(240, 200, 80))
    img.save(sys.argv[1], "JPEG", quality=85)
except Exception:
    open(sys.argv[1], "wb").write(bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070707"
        "0909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c28"
        "37292c30313434341f27393d38323c2e333432ffc0000b0801000100010111ffc40014"
        "0001000000000000000000000000000000ffc40014100100000000000000000000000"
        "0000000000000ffda0008010100003f00b7ffd9"))
PY
FILE_SIZE=$(wc -c < "$TMP_JPG" | tr -d ' ')
HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$TMP_JPG")

# ---------- 2. 登录 ----------
say "2. 登录"
TOKEN=$(curl -sS -X POST "$API/auth/wechat" -H "Content-Type: application/json" \
  -d "{\"code\":\"$CODE\",\"nickname\":\"生成测试\"}" | json_get "['access_token']")

# ---------- 3. 上传 ----------
say "3. 上传原图"
SIGN=$(curl -sS -X POST "$API/photos/upload-url" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
UPLOAD_URL=$(echo "$SIGN" | json_get "['upload_url']")
OSS_KEY=$(echo "$SIGN" | json_get "['oss_key']")
[[ "$UPLOAD_URL" == /* ]] && UPLOAD_URL="${API}${UPLOAD_URL}"
curl -sS -o /dev/null -X PUT "$UPLOAD_URL" -H "Content-Type: image/jpeg" --data-binary "@$TMP_JPG"
CREATE=$(curl -sS -X POST "$API/photos" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"oss_key\":\"$OSS_KEY\",\"hash\":\"$HASH\",\"size_bytes\":$FILE_SIZE,\"mime_type\":\"image/jpeg\"}")
PHOTO_ID=$(echo "$CREATE" | json_get "['id']")
echo "   photo_id=$PHOTO_ID"

# ---------- 4. 等原图处理完 ----------
say "4. 等 worker 处理完原图"
sleep 6

# ---------- 5. 查询配额 ----------
say "5. 查询今日配额"
curl -sS "$API/skills/_/quota" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

# ---------- 6. 拉 Skill 列表 ----------
say "6. 拉 Skill 列表（含官方）"
SKILL_LIST=$(curl -sS "$API/skills" -H "Authorization: Bearer $TOKEN")
echo "$SKILL_LIST" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for s in d[:5]:
    tag = '[官方]' if s['is_official'] else '[我的]'
    print(f\"  {tag} {s['name']}  · use_count={s['use_count']}\")
print(f\"  ...共 {len(d)} 个\")
"
SKILL_ID=$(echo "$SKILL_LIST" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")
SKILL_NAME=$(echo "$SKILL_LIST" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['name'])")
echo "   → 用第一个：$SKILL_NAME"

# ---------- 7. 发起生成 ----------
say "7. 发起 AI 改造"
GEN_RESP=$(curl -sS -X POST "$API/photos/$PHOTO_ID/generate" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"skill_id\":\"$SKILL_ID\"}")
echo "$GEN_RESP" | python3 -m json.tool
GEN_ID=$(echo "$GEN_RESP" | json_get "['id']")

# ---------- 8. 轮询 ----------
say "8. 轮询生成状态（最多 $MAX_WAIT 秒）"
START=$(date +%s)
STATUS="pending"
while true; do
  NOW=$(date +%s); EL=$((NOW - START))
  (( EL > MAX_WAIT )) && { echo "   超时"; exit 2; }
  RESP=$(curl -sS "$API/generations/$GEN_ID" -H "Authorization: Bearer $TOKEN")
  STATUS=$(echo "$RESP" | json_get "['status']")
  printf "   %3ds  status=%s\n" "$EL" "$STATUS"
  if [[ "$STATUS" == "done" ]]; then
    echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print()
print('  ✓ 结果 URL:', d.get('result_url'))
print('  ✓ 模型   :', d.get('model'))
print('  ✓ 成本   :', d.get('cost_yuan'), '元')
"
    break
  fi
  if [[ "$STATUS" == "failed" ]]; then
    ERR=$(echo "$RESP" | json_get "['error_message']")
    echo "   worker 失败: $ERR"
    exit 3
  fi
  sleep 3
done

printf "\n\033[1;32m✅ D15–D17 AI 改造闭环全部通过。\033[0m\n"
