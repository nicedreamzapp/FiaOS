# Contributing

Thanks for taking a look. FiaOS runs on a small set of my own machines, so reports from other Macs,
Windows PCs and network setups are genuinely useful even if you never send code.

## Reporting a problem

Open an [issue](https://github.com/nicedreamzapp/FiaOS/issues/new/choose). The things that make it
quick to fix:

- the OS and version of the machine running the server (Mac chip, or Windows 10 or 11)
- your Python version
- the browser and device you connect from
- whether you reach it on the LAN, or through the reverse tunnel and `deploy/nginx-fia.conf`
- the server output around the time it went wrong

## Running it locally

macOS:

```bash
git clone https://github.com/nicedreamzapp/FiaOS.git
cd FiaOS
python3.12 -m venv .venv
.venv/bin/pip install aiohttp psutil numpy anthropic pillow pyobjc-framework-Quartz
FIAOS_PASSWORD='your-strong-password' .venv/bin/python3 server.py
# open http://localhost:9000
```

Windows: follow the steps in the README, then run `.\run_fiaos.ps1` from the `windows` folder.

## Tests

The pytest suite runs anywhere, Linux included, with no Mac, GPU or network. CI runs it on every
push and pull request.

```bash
python -m venv .venv-test
.venv-test/bin/pip install -r requirements-test.txt
.venv-test/bin/python -m pytest
```

CI also runs `flake8 --select=E9,F63,F7,F82 .`. See [tests/README.md](tests/README.md) for what each
file covers and for the older live scripts (`tests/test_auth.py` and `tests/test_infra.sh`).

## Pull requests

Small and focused is easiest to review. Say which OS you tested on. Changes to login, sessions or
the terminal need tests. For big changes, open an issue first so we can talk it through.

Please follow the [code of conduct](CODE_OF_CONDUCT.md).
