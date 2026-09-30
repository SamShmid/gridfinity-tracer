"""Test session setup.

1. Point GT_DATA_DIR at a fresh temp dir BEFORE app.config is imported anywhere, so the API tests never
   touch the real data volume (setdefault: an explicit env var still wins).
2. onnxruntime / rembg sessions can abort ("recursive_mutex lock failed") during interpreter teardown
   after the tests have already passed. Exit hard once pytest has printed its summary so the process
   exit code reflects the test result, not the teardown crash."""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GT_DATA_DIR", tempfile.mkdtemp(prefix="gt-test-data-"))


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # Outermost wrapper: everything after `yield` runs once the terminal reporter has printed the
    # failure summary, so the hard exit no longer hides it.
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(exitstatus))
