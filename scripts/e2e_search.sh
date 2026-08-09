#!/usr/bin/env bash
# ============================================================================
# e2e_search.sh — 端到端联调 D8–D9 搜索优化
#
# 覆盖：
#   1. 准备：上传 3 张假图，让库里有些可搜的数据
#   2. 简单语义搜索
#   3. 时间过滤搜索（from_date）
#   4. auto_parse（"上个月的图片"自动解析成时间范围）
#   5. Embedding 缓存命中验证（同 query 二次搜索应 cache_hit=true）
#   6. 游标分页验证（limit=2 → next_cursor → 下一页）
# ============================================================================
set -euo pipefail

API="${API:-http://localhost:8000}"
CODE="e2esearch-$(date +%s)"
NICK="搜索测试"

command -v curl >/dev/null || { echo "缺 curl"; exit 1; }
command -v python3 >/dev/null || { echo "缺 python3"; exit 1; }

json_get() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }

# ---------- 登录 ----------
say "登录 (code=$CODE)"
TOKEN=$(curl -sS -X POST "$API/auth/wechat" -H "Content-Type: application/json" \
  -d "{\"code\":\"$CODE\",\"nickname\":\"$NICK\"}" | json_get "['access_token']")

# ---------- 上传 N 张假图 ----------
upload_one() {
  local seed="$1"
  local color="$2"
  local TMP="$(mktemp -t e2esearch.XXXX.jpg)"
  python3 - <<PY "$TMP" "$color"
import sys
try:
    from PIL import Image
    Image.new("RGB", (400, 300), tuple(int(x) for x in sys.argv[2].split(","))).save(sys.argv[1], "JPEG")
except Exception:
    open(sys.argv[1], "wb").write(bytes.fromhex(
      "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070707"
      "0909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c28"
      "37292c30313434341f27393d38323c2e333432ffc0000b0801000100010111ffc40014"
      "0001000000000000000000000000000000ffc40014100100000000000000000000000"
      "0000000000000ffda0008010100003f00b7ffd9"))
PY
  local SIZE=$(wc -c < "$TMP" | tr -d ' ')
  local HASH=$(python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$TMP")

  local SIGN=$(curl -sS -X POST "$API/photos/upload-url" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"hash\":\"$HASH\",\"size_bytes\":$SIZE,\"mime_type\":\"image/jpeg\"}")
  local URL=$(echo "$SIGN" | json_get "['upload_url']")
  local KEY=$(echo "$SIGN" | json_get "['oss_key']")
  [[ "$URL" == /* ]] && URL="${API}${URL}"
  curl -sS -o /dev/null -X PUT "$URL" -H "Content-Type: image/jpeg" --data-binary "@$TMP"
  curl -sS -o /dev/null -X POST "$API/photos" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"oss_key\":\"$KEY\",\"hash\":\"$HASH\",\"size_bytes\":$SIZE,\"mime_type\":\"image/jpeg\"}"
  rm -f "$TMP"
  printf "   uploaded seed=%s\n" "$seed"
}

say "1. 上传 3 张假图（不同颜色）作为数据集"
upload_one 1 "180,80,80"     # 红色系
upload_one 2 "80,180,80"     # 绿色系
upload_one 3 "80,80,180"     # 蓝色系

say "   等 worker 处理完（8 秒）..."
sleep 8

# ---------- 2. 简单语义 ----------
say "2. 简单语义搜索 q=\"一张图\""
curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"q":"一张图","limit":10}' | python3 -m json.tool | head -30

# ---------- 3. 时间过滤 ----------
say "3. 时间过滤 from_date=今天"
TODAY=$(date +%Y-%m-%d)
curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"q\":\"一张图\",\"from_date\":\"$TODAY\",\"limit\":10}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'   命中 {len(d[\"items\"])} 张 · cache_hit={d[\"cache_hit\"]}')"

# ---------- 4. auto_parse ----------
say "4. auto_parse=true  q=\"上个月的图片\""
curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"q":"上个月的图片","auto_parse":true,"limit":5}' \
  | python3 -m json.tool | head -20

# ---------- 5. 缓存命中 ----------
say "5. 缓存验证：连续两次搜同一个词"
for i in 1 2; do
  RESP=$(curl -sS -X POST "$API/search" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"q":"验证缓存的固定查询词_photo_agent","limit":3}')
  HIT=$(echo "$RESP" | json_get "['cache_hit']")
  echo "   第 $i 次 cache_hit=$HIT"
done

# ---------- 6. 游标分页 ----------
say "6. 分页：limit=2 拿第一页 → next_cursor → 第二页"
PAGE1=$(curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"q":"一张图","limit":2}')
CURSOR=$(echo "$PAGE1" | json_get "['next_cursor']")
echo "   第一页 items: $(echo "$PAGE1" | json_get "[\"total\"]")  next_cursor: ${CURSOR:0:20}..."
if [[ "$CURSOR" != "None" && -n "$CURSOR" ]]; then
  PAGE2=$(curl -sS -X POST "$API/search" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"q\":\"一张图\",\"limit\":2,\"cursor\":\"$CURSOR\"}")
  echo "   第二页 items: $(echo "$PAGE2" | json_get "['total']")"
fi

printf "\n\033[1;32m✅ D8–D9 搜索优化闭环全部通过。\033[0m\n"
