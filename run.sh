#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi

if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    echo "Virtual environment has no pip; recreating .venv..."
    python3 -m venv --clear .venv
fi

if [ ! -f .venv/.installed-v2 ]; then
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
    touch .venv/.installed-v2
fi

exec .venv/bin/python run.py
