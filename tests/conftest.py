"""Shared fixtures for arkos tests."""

import os
import sys

# Ensure project root is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set required env vars that some modules read at import time
os.environ.setdefault("DB_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key")
