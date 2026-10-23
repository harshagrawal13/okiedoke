#!/bin/sh
# Install the app's Python environment on Kamaji. Run once after cloning the repo.
set -eu
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install .
