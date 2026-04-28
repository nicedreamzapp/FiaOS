#!/bin/zsh
# Start PersonaPlex with a persona prompt.
# NOTE: Keep prompts SHORT — long prompts can cause garbled audio.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Persona system prompt. Set FIA_PROMPT env var to override.
FIA_PROMPT="${FIA_PROMPT:-You are a helpful local voice assistant. Keep replies short.}"

exec .venv/bin/python3 -u -m personaplex_mlx.local_web -q 4 --no-browser \
    --voice NATF0 \
    --text-prompt "$FIA_PROMPT" \
    --text-temp 0.1 \
    --audio-temp 0.5
