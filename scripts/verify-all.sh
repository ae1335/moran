#!/usr/bin/env bash
# 全量验证：单测 → 前端构建 → 起服务 → API 冒烟 → 浏览器 UI 冒烟 → PWA/夜审/索引端到端 → 定时任务实跑。
# 用法：bash scripts/verify-all.sh [端口]
set -uo pipefail
PORT="${1:-8791}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
export PYTHONPATH=vendor/pylib:.
FAIL=0

step() { printf '\n=== %s ===\n' "$1"; }

step "1/7 Python 单测"
python3 -m pytest tests/ -q 2>&1 | tail -2 || FAIL=1

step "2/7 前端构建"
(cd web && npx vite build 2>&1 | tail -3) || FAIL=1

# --- 真实书库基线（缺陷 #51 的第二道防线）-------------------------------
# 第 3~5 步是唯一直接对真实 ws 起服务的环节。历史上这里曾因端口抢占而测错对象，
# 被测服务在被外部操作时把章纲/审稿写进了真实书库。现在无论谁写的，只要这期间
# 真实书库有任何一个字节变了，就立刻失败并把差异打出来——静默污染必须变成响亮的红。
BOOK_REL="ws/novels/novel-《凡人问道》"
book_hash() { ( cd "$BOOK_REL" && find . -path ./.git -prune -o -type f -print 2>/dev/null | sort | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1 ); }
BOOK_H0=$(book_hash)
BOOK_GIT0=$(git -C "$BOOK_REL" status --porcelain 2>/dev/null)

step "3/7 启动服务 :$PORT"

# --- 端口预检（缺陷 #51 的回归防线）--------------------------------------
# 曾经发生过：上一轮遗留的 serve 进程仍占着 $PORT，本步骤绑定失败只往日志里写了
# 一行 Errno 98，而下面的健康检查请求落到了**那个残留服务**上并返回 ok,
# 于是后面 4、5 步全程测的是「别人起的、指向真实书库的服务」，最后还 RESULT=PASS。
# 假绿比红更危险，所以这里先确认端口确实是空的，再起自己的服务。
port_busy() { # 端口被占用返回 0
  python3 - "$1" <<'PY'
import socket, sys
p = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", p))
except OSError:
    sys.exit(0)          # 已占用
finally:
    s.close()
sys.exit(1)              # 空闲
PY
}

if port_busy "$PORT"; then
  echo "端口 $PORT 已被占用，拒绝继续：健康检查会命中别人的服务，导致本次验证测错对象。"
  echo "  排查：ps aux | grep '[s]erver.cli'   # 找出占着端口的残留服务"
  echo "  或者换端口：bash scripts/verify-all.sh $((PORT+10))"
  printf '\nRESULT=FAIL\n'
  exit 1
fi

mkdir -p .runtime-test
SERVE_LOG=".runtime-test/verify-serve.log"
python3 -m server.cli --root ws serve --port "$PORT" >"$SERVE_LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

HEALTH=""
for _ in $(seq 1 30); do
  HEALTH=$(curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/health" 2>/dev/null) && break
  sleep 0.5
done
if [ -z "$HEALTH" ]; then
  echo "服务未起来（日志末 20 行）："
  tail -20 "$SERVE_LOG" 2>/dev/null
  printf '\nRESULT=FAIL\n'
  exit 1
fi
echo "$HEALTH"

# --- 服务身份校验：健康检查里回显的 root 必须是本次起的 ws -------------------
# 光看「有个健康响应」是不够的，还得证明它就是我们要测的那个根目录。
GOT_ROOT=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("root",""))' <<<"$HEALTH" 2>/dev/null)
WANT_ROOT=$(realpath -m "$HERE/ws")
if [ "$GOT_ROOT" != "$WANT_ROOT" ]; then
  echo "服务身份不符：health.root=$GOT_ROOT，期望 $WANT_ROOT"
  echo "  说明 :$PORT 上的服务不是本次启动的，继续跑只会得到假绿。"
  printf '\nRESULT=FAIL\n'
  exit 1
fi
echo "服务身份已校验：root=$GOT_ROOT"
echo

step "4/7 API 冒烟"
BOOK=$(python3 -c "import urllib.parse;print(urllib.parse.quote('novel-《凡人问道》'))")
for ep in "shelf" "books/$BOOK/chapters" "books/$BOOK/daily-panel" "books/$BOOK/foreshadow" "books/$BOOK/timeline" "books/$BOOK/names" "books/$BOOK/export/precheck" "providers" "usage?days=7"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/api/$ep")
  printf '  %-46s %s\n' "$ep" "$code"
  [ "$code" = "200" ] || FAIL=1
done
# 起名生成（POST）也验一下
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/api/books/$BOOK/names" \
  -H 'content-type: application/json' -d '{"kind":"character","count":3,"seed":1,"save":false}')
printf '  %-46s %s\n' "POST books/.../names" "$code"
[ "$code" = "200" ] || FAIL=1

# 未知 API 必须是 404 JSON，不能是 HTML
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/nope")
[ "$code" = "404" ] || { echo "  /api/nope 应为 404，实际 $code"; FAIL=1; }

step "5/7 浏览器 UI 冒烟"
node scripts/ui-smoke.mjs "http://127.0.0.1:$PORT" 2>&1 | tail -3 || FAIL=1

# --- 真实书库必须逐字节未变 -----------------------------------------------
BOOK_H1=$(book_hash)
BOOK_GIT1=$(git -C "$BOOK_REL" status --porcelain 2>/dev/null)
if [ "$BOOK_H0" != "$BOOK_H1" ]; then
  echo "真实书库在 3~5 步期间被改写（基线 $BOOK_H0 -> 现在 $BOOK_H1）"
  echo "  git status 变化："
  diff <(echo "$BOOK_GIT0") <(echo "$BOOK_GIT1") | sed 's/^/    /'
  echo "  说明：读路径本不该写盘，出现差异说明有别人（或残留服务）在动这个库。"
  FAIL=1
else
  echo "真实书库逐字节未变（$BOOK_H1）"
fi

# 隔离模式：脚本自己复制一份 ws 并起临时服务，演示书 git 零污染。
step "6/7 PWA / 夜审 / 索引漂移 端到端"
node scripts/pwa-smoke.mjs 2>&1 | tail -6 || FAIL=1

# 定时任务端到端：真起服务 + 真等一次 cron 触发（约 20-70s）。
# 单测只覆盖 job 函数与注册逻辑，覆盖不到「服务装配后 cron 真的会调起来」——
# 那条路径曾经因为 fastapi 移除 add_event_handler 而整体崩掉，且默认配置关着，长期隐身。
step "7/7 定时任务实跑（真实 cron 触发）"
bash scripts/scheduler-smoke.sh "$((PORT+1))" > .runtime-test/sched.log 2>&1 || FAIL=1
tail -24 .runtime-test/sched.log

printf '\n'
if [ "$FAIL" -eq 0 ]; then echo "RESULT=PASS"; else echo "RESULT=FAIL"; fi
exit "$FAIL"
