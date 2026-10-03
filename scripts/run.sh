#!/bin/bash
# 開発用の起動スクリプト。
# Vagrant のポートフォワード（guest 8002 → host 8002）で使うため、0.0.0.0 で待ち受ける。
# 127.0.0.1 だけにバインドするとホスト側のブラウザから開けない。
cd "$(dirname "$0")/.."
exec python3.9 -m flask --app backend.app run \
  --host "${HOST:-0.0.0.0}" --port "${PORT:-8002}" "$@"
