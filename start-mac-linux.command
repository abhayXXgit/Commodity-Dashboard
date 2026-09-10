#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# TransformerProcure Intelligence — one-click start (macOS / Linux)
# Double-click this file. First run takes a few minutes to install; after
# that it starts in seconds.
# ---------------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

echo ""
echo "  TransformerProcure Intelligence"
echo "  ==============================="
echo ""

# find a usable python
PY=""
for c in python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
    if [ "$v" -ge 310 ] 2>/dev/null; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "  Python 3.10 or newer was not found."
  echo "  Install it from https://www.python.org/downloads/ and run this again."
  echo ""
  read -p "  Press Enter to close..."
  exit 1
fi
echo "  Using $($PY --version)"

if [ ! -d venv ]; then
  echo "  First run — setting up (this takes 2-3 minutes)..."
  "$PY" -m venv venv
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet -r requirements.txt
  echo "  Setup complete."
fi

echo ""
echo "  Starting. The dashboard will open at http://127.0.0.1:8000"
echo "  Leave this window open while you use it. Press Ctrl+C to stop."
echo ""

( sleep 4; command -v open >/dev/null && open http://127.0.0.1:8000 || \
  command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:8000 ) >/dev/null 2>&1 &

./venv/bin/python backend/main.py
