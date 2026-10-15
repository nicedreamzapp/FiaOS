---
title: FiaOS credits - the open source projects it is built on
description: The upstream projects FiaOS uses and credits, including xterm.js, aiohttp, psutil, pywinpty, PyObjC, Pillow, NumPy, nginx, OpenSSH, Anthropic Claude Code, Ollama, and the voice-era MLX and PersonaPlex work.
---

# Credits

FiaOS is written by [Matt Macosko](https://github.com/nicedreamzapp). The FiaOS servers, screen pipeline, Windows port, web UI, deploy configs and tests are in this repo. Everything below is other people's work, used under its own license.

## Used by FiaOS today

| Project | What it does here |
|---|---|
| [xterm.js](https://xtermjs.org/) and its fit addon | The terminal in the browser. Vendored in `static/vendor/`. |
| [aiohttp](https://docs.aiohttp.org/) | The web server, WebSockets, and the Windows machine proxy. |
| [psutil](https://psutil.readthedocs.io/) | CPU, memory, disk, battery, network and process info. |
| [pywinpty](https://github.com/andfoy/pywinpty) | ConPTY terminal on Windows. |
| [PyObjC](https://pyobjc.readthedocs.io/) (`pyobjc-framework-Quartz`) | Screen capture and mouse/keyboard events on macOS. |
| [Pillow](https://python-pillow.org/) | JPEG encoding of screen tiles. |
| [NumPy](https://numpy.org/) | Frame slicing and tiling. |
| [nginx](https://nginx.org/) | HTTPS and cookie-based routing in the reference deployment. |
| [OpenSSH](https://www.openssh.com/) | Reverse tunnels from each machine to the VPS. |
| [Claude Code](https://claude.com/claude-code) by Anthropic | Called as `claude -p` by the natural-language helper and `/api/claude_stream`, and the main thing people run in the Terminal tab. FiaOS doesn't include it. |
| [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) | Imported by the macOS `executor.py`. |
| [Ollama](https://ollama.com/) and the `qwen3-coder` model | Local fallback for the natural-language helper. |
| [noVNC](https://novnc.com/) | Optional browser VNC client for the Windows `/vnc` route. Not bundled; you supply it. |

## Voice-era work (from CREDITS.md)

These were used by the voice feature that has since been removed from FiaOS. They're still credited in [`CREDITS.md`](https://github.com/nicedreamzapp/FiaOS/blob/main/CREDITS.md), and `requirements.txt` / `pyproject.toml` still list some of them.

| Project | By |
|---|---|
| [MLX](https://github.com/ml-explore/mlx) | Apple's ml-explore team |
| PersonaPlex on MLX | the upstream PersonaPlex authors |
| [rustymimi / sphn](https://github.com/kyutai-labs) | Kyutai Labs |
| [SentencePiece](https://github.com/google/sentencepiece) | Google |
| [huggingface-hub](https://github.com/huggingface/huggingface_hub) | Hugging Face |
| [blessed](https://github.com/jquast/blessed) | Jeff Quast |

If your work is used here and isn't listed, or you'd like the wording changed, [open an issue](https://github.com/nicedreamzapp/FiaOS/issues).

## License

FiaOS is released under the [MIT License](https://github.com/nicedreamzapp/FiaOS/blob/main/LICENSE).
