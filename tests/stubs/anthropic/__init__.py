"""Empty stand-in for the Anthropic SDK, used only by the test suite.

The macOS executor.py imports `anthropic` but never calls it (it shells out to
the `claude` CLI), so tests that spawn server.py in a subprocess put this
directory on PYTHONPATH instead of installing the real SDK. A real install
takes precedence only if it comes first on sys.path; this stub is harmless
either way.
"""
