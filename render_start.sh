#!/usr/bin/env bash
# One Render web service: FastAPI on localhost, Streamlit on the public port.
set -euo pipefail
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
for _ in $(seq 1 30); do curl -sf http://127.0.0.1:8000/health >/dev/null && break; sleep 1; done
exec python -m streamlit run dashboard/streamlit_app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false
