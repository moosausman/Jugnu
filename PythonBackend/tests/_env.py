"""Imported first by every test module: isolates all persistent state."""

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if not os.environ.get("JUGNU_TEST_DATA_DIR"):
    os.environ["JUGNU_TEST_DATA_DIR"] = tempfile.mkdtemp(prefix="jugnu-test-")
os.environ["JUGNU_DATA_DIR"] = os.environ["JUGNU_TEST_DATA_DIR"]
# No network embeddings, no awareness subprocesses, no real model calls.
os.environ.setdefault("JUGNU_ENABLE_EMBEDDINGS", "0")
os.environ.setdefault("JUGNU_ENABLE_AWARENESS", "0")
os.environ.setdefault("JUGNU_PROVIDER", "api")
os.environ.setdefault("JUGNU_API_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("JUGNU_API_CHAT_MODEL", "test-model")
os.environ.setdefault("JUGNU_API_KEY", "test-key-not-real-0000")
