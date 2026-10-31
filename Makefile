# 网文工作台 · 常用命令
PY := PYTHONPATH=vendor/pylib:. python3
WS ?= ws

.PHONY: help install test build serve init demo verify smoke-pwa smoke-scheduler clean

help:
	@echo "make install   安装 Python(到 vendor/pylib) + 前端依赖"
	@echo "make test      跑全部单测"
	@echo "make build     构建前端到 web/dist"
	@echo "make serve     启动 Web 服务 (127.0.0.1:8765)"
	@echo "make demo      在工作区 $(WS) 里重建 8 章离线 demo（mock，不联网）"
	@echo "make verify    全量验收（单测→构建→API→UI→PWA/夜审/索引→定时任务实跑），末行 RESULT=PASS"
	@echo "make smoke-pwa         只跑 PWA 离线队列 / 夜审 / 索引漂移 端到端（32 项）"
	@echo "make smoke-scheduler   只跑定时任务真实 cron 触发（15 项，约 20-90s）"

install:
	pip install --target vendor/pylib -r requirements.txt
	cd web && npm install

test:
	$(PY) -m pytest tests/ -q

build:
	cd web && npx vite build

serve:
	$(PY) -m server.cli --root $(WS) serve --port 8765

init:
	$(PY) -m server.cli --root $(WS) init

demo:
	bash scripts/make-demo.sh $(WS)

# 全量验收：唯一的「做完没」判据。所有端到端都跑在 .runtime-test/ 的 ws 副本上，
# 不碰真实书库（被测服务自己会写 git，直连真库会造出 commit）。
verify:
	bash scripts/verify-all.sh

smoke-pwa:
	node scripts/pwa-smoke.mjs

smoke-scheduler:
	bash scripts/scheduler-smoke.sh

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache web/dist
