#!/usr/bin/env bash
# 一键重建离线 demo（mock 模型，不联网）：init → 建书 → 8 章流水线 → 导出。
# 用法：bash scripts/make-demo.sh [工作区目录]
set -euo pipefail
ROOT="${1:-ws}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
export PYTHONPATH=vendor/pylib:.

rm -rf "$ROOT"
python3 -m server.cli --root "$ROOT" init >/dev/null
cat > "$ROOT/workbench-config/providers.yaml" <<'YAML'
providers:
  - name: mock-local
    kind: mock
    bridge: mock
    models: ["mock-1"]
    enabled: true
    note: "离线演示/回归基线，不联网"
YAML
cat > "$ROOT/workbench-config/routing.yaml" <<'YAML'
routes:
  plan: ["mock-local:mock-1"]
  write: ["mock-local:mock-1"]
  review: ["mock-local:mock-1"]
  curator: ["mock-local:mock-1"]
  polish: ["mock-local:mock-1"]
  summarize: ["mock-local:mock-1"]
  deconstruct: ["mock-local:mock-1"]
  free: ["mock-local:mock-1"]
YAML
python3 -m server.cli --root "$ROOT" new "凡人问道" >/dev/null
python3 -m server.cli --root "$ROOT" pipeline --chapters "${CHAPTERS:-1-8}"
python3 -m server.cli --root "$ROOT" export --mode 合集
echo "demo 就绪：PYTHONPATH=vendor/pylib:. python3 -m server.cli --root $ROOT serve --port 8791"
