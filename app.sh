#!/bin/zsh
set -e

APP_ROOT="${0:A:h}"
cd "$APP_ROOT"
# VS Code 같은 Electron 앱 안에서 띄우면 이 값이 물려와 Electron이 Node로 실행된다.
unset ELECTRON_RUN_AS_NODE

if [[ ! -x "$APP_ROOT/.venv/bin/python" ]]; then
  print "처음 한 번만 엔진 Python 환경을 준비합니다..."
  uv sync
fi

if [[ ! -x "$APP_ROOT/node_modules/.bin/electron" ]]; then
  print "처음 한 번만 Electron 실행 환경을 준비합니다..."
  npm install --no-audit --no-fund
fi

exec "$APP_ROOT/node_modules/.bin/electron" "$APP_ROOT/electron-app/main.mjs"
