"""
Shared pytest configuration for all backend tests.

Layout
------
tests/
  unit/        - Fully mocked, no external I/O. Run anywhere (host or Docker).
  integration/ - Require the full Docker stack (DB, Redis, ChromaDB).
                 Always run via: docker compose exec backend pytest -m integration

Path setup
----------
Adds backend/ to sys.path so `import app.xxx` resolves without an editable
install. Works both on the host machine and inside the Docker container
(where /app is the working directory and app/ is already importable).

Environment stubs
-----------------
Injects dummy values for all required pydantic-settings fields before any
module is imported. Integration tests that need real credentials get them
from the container's actual .env (already loaded by Docker).
"""

import os
import sys
from pathlib import Path

# backend/ directory — parent of the `app` package.
# Works from any subdirectory (unit/, integration/, or tests/ root).
_BACKEND_DIR = Path(__file__).parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Stub values for required Settings fields so unit tests never need real
# credentials. Integration tests run inside Docker where the real .env is
# already loaded — these setdefault() calls are no-ops in that case.
_REQUIRED_ENV_STUBS = {
    "OPENAI_API_KEY": "sk-test-stub",
    "DB_USER": "test_user",
    "DB_PASSWORD": "test_pass",
    "DB_HOST": "localhost",
    "DB_NAME": "ipl_db",
}
for _key, _val in _REQUIRED_ENV_STUBS.items():
    os.environ.setdefault(_key, _val)
