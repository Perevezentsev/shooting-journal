#!/usr/bin/env bash
# Запуск журнала: ./run.sh, потом открыть http://localhost:8765
cd "$(dirname "$0")"
source .venv/bin/activate
exec uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
