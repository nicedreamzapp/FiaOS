#!/bin/zsh
# FiaOS server launcher — used by launchd (or run directly).
# Resolves its own directory so it works no matter where the project lives.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec .venv/bin/python3 -u server.py
