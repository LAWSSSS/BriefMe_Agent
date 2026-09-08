#!/usr/bin/env bash
# Idempotent setup for the BriefMe Agent dev environment.
# Safe to run repeatedly: it only installs what is missing.
set -euo pipefail

cd "$(dirname "$0")/.."

# Python's `venv` module needs the distro package on Debian/Ubuntu images.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y python3-venv
  else
    apt-get update -qq
    apt-get install -y python3-venv
  fi
fi

if [ ! -x "venv/bin/python" ]; then
  python3 -m venv venv
fi

venv/bin/python -m pip install --upgrade pip
venv/bin/pip install -r requirements.txt
venv/bin/pip install -r requirements-dev.txt

echo "BriefMe environment ready. Run: ./venv/bin/python app.py"
