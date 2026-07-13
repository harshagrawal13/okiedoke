#!/bin/zsh
# Double-clickable launcher: starts the okiedoke server and opens the app.
cd "$(dirname "$0")" && exec python3 serve.py
