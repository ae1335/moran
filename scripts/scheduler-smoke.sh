#!/usr/bin/env bash
# 定时任务端到端：真起服务、真等一次 cron 触发、真看任务副作用与日志。
#
# 为什么这个脚本存在：docs/VERIFICATION.md 曾自认「定时任务未实跑」，而那条路径里
# 藏着 4 个缺陷——同秒备份必失败(#46)、失败无日志(#47)、DEFAULTS 被共享出去(#48)、
# 以及最致命的 fastapi 0.162 移除 add_event_handler 导致 enabled:true 时服务根本起不来(#45)。
# 默认配置把 enabled 关掉，所以问题长期隐身。这个脚本专门盯着「开启后」的那条路。
#
# 隔离：整个脚本跑在 .runtime-test/ 下的 ws 副本上，不碰真实书库（被测服务自己会写 git）。
# 用法：bash scripts/scheduler-smoke.sh [端口]
set -uo pipefail
PORT="${1:-8802}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"
export PYTHONPATH=vendor/pylib:.

TMP=".runtime-test/scheduler-smoke"
WS="$TMP/ws"
LOG="$TMP/server.log"
BOOK="novel-《凡人问道》"
OUTLINE="$WS/novels/$BOOK/outline/chapters/ch-0001.json"

PASS=0
FAILN=0
check() { # check <描述> <命令...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  [通过] %s\n' "$desc"; PASS=$((PASS+1))
  else
    printf '  [失败] %s\n' "$desc"; FAILN=$((FAILN+1))
  fi
}

# ---------- 真实书库基线（跑完必须逐字一致）----------
REAL_HEAD_BEFORE=$(git -C "ws/novels/$BOOK" rev-parse HEAD 2>/dev/null)
REAL_STATUS_BEFORE=$(git -C "ws/novels/$BOOK" status --porcelain 2>/dev/null)

SRV=""
cleanup() {
  if [ -n "$SRV" ]; then kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "=== 准备隔离工作区 ==="
rm -rf "$TMP"
mkdir -p "$TMP"
cp -a ws "$WS" || { echo "复制 ws 失败"; echo "RESULT=FAIL"; exit 1; }
rm -rf "$WS/backups" "$WS/serve.log"

# 把第 1 章置为 drafted —— 夜审只处理 drafted 章，这是它的输入前提
python3 - "$OUTLINE" <<'PY' || { echo "改章纲失败"; echo "RESULT=FAIL"; exit 1; }
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
d["status"] = "drafted"
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

# 开启定时任务：每分钟触发，两个 job 都开，逼出「同秒连跑」与真实 cron 路径
mkdir -p "$WS/workbench-config"
cat > "$WS/workbench-config/scheduler.yaml" <<'YAML'
enabled: true
backup_dir: backups
backup_keep: 3
jobs:
  nightly_review:
    enabled: true
    cron: "* * * * *"
  index_rebuild:
    enabled: false
  backup:
    enabled: true
    cron: "* * * * *"
YAML

echo "=== 起服务（scheduler enabled: true）==="

# 端口预检（缺陷 #51）：残留 serve 进程会让下面的健康检查命中别人的服务，
# 于是整轮断言（包括「章纲变成 reviewed」）可能由**另一个库**满足，还报 PASS。
port_busy() {
  python3 - "$1" <<'PY'
import socket, sys
p = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", p))
except OSError:
    sys.exit(0)
finally:
    s.close()
sys.exit(1)
PY
}
if port_busy "$PORT"; then
  echo "端口 $PORT 已被占用，拒绝继续：健康检查会命中别人的服务，导致本次验证测错对象。"
  echo "  换端口：bash scripts/scheduler-smoke.sh $((PORT+10))"
  echo "RESULT=FAIL"
  exit 1
fi

python3 -m server.cli --root "$WS" serve --port "$PORT" > "$LOG" 2>&1 &
SRV=$!
UP=0
HEALTH=""
for _ in $(seq 1 40); do
  HEALTH=$(curl -sf --max-time 1 "http://127.0.0.1:$PORT/api/health" 2>/dev/null) && { UP=1; break; }
  sleep 0.5
done
if [ "$UP" != "1" ]; then
  echo "服务未起来（这正是缺陷 #45 的症状），日志尾部："
  tail -25 "$LOG"
  echo "RESULT=FAIL"
  exit 1
fi

# 服务身份校验：health 回显的 root 必须就是本脚本刚准备的临时 ws。
GOT_ROOT=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("root",""))' <<<"$HEALTH" 2>/dev/null)
WANT_ROOT=$(realpath -m "$WS")
if [ "$GOT_ROOT" != "$WANT_ROOT" ]; then
  echo "服务身份不符：health.root=$GOT_ROOT，期望 $WANT_ROOT"
  echo "  说明 :$PORT 上的服务不是本次启动的，继续跑只会得到假绿。"
  echo "RESULT=FAIL"
  exit 1
fi
echo "  服务已就绪 :$PORT（root 已校验）"

echo "=== 断言 ==="

# 1) 开启后调度器真的装配出来（缺陷回归：此前这里直接 AttributeError 崩掉）
INFO=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/jobs/scheduler")
check "调度器可用（available=true）" bash -c "echo '$INFO' | python3 -c \"import json,sys;sys.exit(0 if json.load(sys.stdin).get('available') is True else 1)\""
check "调度器已启用（enabled=true）" bash -c "echo '$INFO' | python3 -c \"import json,sys;sys.exit(0 if json.load(sys.stdin).get('enabled') is True else 1)\""
check "注册了 nightly_review 与 backup" bash -c "echo '$INFO' | python3 -c \"import json,sys;j=[x.get('job') for x in json.load(sys.stdin).get('jobs',[])];sys.exit(0 if j==['nightly_review','backup'] else 1)\""
check "调度器真的 start 了（日志有 Scheduler started）" grep -q "Scheduler started" "$LOG"

# 2) 真等一次 cron 触发：夜审把 drafted 推成 reviewed
# 上限要留足：cron 是每分钟一次，最坏情况要等满一整分钟，再加审稿与落盘时间。
# 90s 与 120s 都实测出现过没等到（verify-all 里第 7 步 14/15）；连跑三次实测触发秒数为 16/59/58，
# 最坏情况接近满一分钟再加审稿落盘，留到 180s。
echo "  等待 cron 触发（最多 180s）..."
FIRED=0
ST=""
for i in $(seq 1 180); do
  ST=$(python3 -c "import json;print(json.load(open('$OUTLINE',encoding='utf-8')).get('status'))" 2>/dev/null)
  if [ "$ST" = "reviewed" ]; then FIRED=$i; break; fi
  sleep 1
done
if [ "$FIRED" != "0" ]; then
  echo "  cron 在第 $FIRED 秒触发"
  check "夜审把第 1 章从 drafted 推进为 reviewed" test "$ST" = "reviewed"
else
  echo "  [失败] 180s 内未观察到 cron 触发（当前状态 $ST）"
  FAILN=$((FAILN+1))
fi

# 3) 备份 job 真落了产物（同秒重名由 _unique_dir 兜住）
check "备份产生了目录" test -d "$WS/backups"
check "备份目录下有 novels 快照" bash -c "ls -1d '$WS'/backups/*/novels >/dev/null 2>&1"
check "备份体积非空" bash -c "test -n \"\$(find '$WS/backups' -type f | head -1)\""

# 4) 成功也留日志（否则 cron 侧彻底静默）
check "日志含「定时任务完成：nightly_review」" grep -q "定时任务完成：nightly_review" "$LOG"
check "日志含「定时任务完成：backup」" grep -q "定时任务完成：backup" "$LOG"

# 5) 全程无崩溃
check "服务日志无 Traceback" bash -c "! grep -q 'Traceback' '$LOG'"
check "服务仍在响应" bash -c "curl -sf --max-time 3 'http://127.0.0.1:$PORT/api/health' >/dev/null"

echo "=== 收尾检查 ==="
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; SRV=""

REAL_HEAD_AFTER=$(git -C "ws/novels/$BOOK" rev-parse HEAD 2>/dev/null)
REAL_STATUS_AFTER=$(git -C "ws/novels/$BOOK" status --porcelain 2>/dev/null)
check "真实书库 HEAD 未被改动" test "$REAL_HEAD_BEFORE" = "$REAL_HEAD_AFTER"
check "真实书库工作区未被改动" test "$REAL_STATUS_BEFORE" = "$REAL_STATUS_AFTER"
rm -rf "$TMP"
check "临时工作区已清理" bash -c "test ! -e '$TMP'"

TOTAL=$((PASS+FAILN))
printf '\n检查项 %d / 通过 %d / 失败 %d\n' "$TOTAL" "$PASS" "$FAILN"
if [ "$FAILN" -eq 0 ]; then echo "RESULT=PASS"; exit 0; else echo "RESULT=FAIL"; exit 1; fi
