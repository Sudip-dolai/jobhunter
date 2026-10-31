#!/bin/sh
# MODE=worker -> background job that scrapes and matches automatically; otherwise the web app.
if [ "$MODE" = "worker" ]; then
  exec python run_pipeline.py --loop --every "${EVERY_MINUTES:-30}"
fi
exec streamlit run streamlit_app.py --server.port="${PORT:-8080}" --server.address=0.0.0.0 --server.headless=true
