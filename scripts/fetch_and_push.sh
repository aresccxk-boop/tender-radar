#!/usr/bin/env bash
# 在国内机器上抓取并推送 data/tenders.json。需要本机已经配置好这个仓库的 git 推送权限。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if [[ ! -d .venv ]]; then
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
FETCH_RUNNER=local-cn .venv/bin/python scripts/fetch.py

git add data/tenders.json
if git diff --cached --quiet; then
  echo "数据没有变化，不推送"
else
  git commit -m "chore: 从国内机器更新招标数据"
  git push
fi
