"""
Unit tests for embedding-model versioning in ChromaDB vector stores.

Covers:
  - cricket_knowledge._content_hash(): determinism, sensitivity to file content
    and to the embedding model name stored in settings.
  - prompts.py hashing logic (_get_few_shot_selector hash): same IPL_EXAMPLES
    but different model names must yield different hashes.
  - config.Settings.openai_embedding_model: correct default value and correct
    override via environment variable.

All external I/O is mocked — no real filesystem reads, no OpenAI calls,
no ChromaDB access.

Test groups
-----------
TestContentHash          — cricket_knowledge._content_hash()
TestFewShotExamplesHash  — inline hash logic mirrored from _get_few_shot_selector()
TestSettingsEmbeddingModel — config.Settings field behaviour
"""

import hashlib
import importlib
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_content_hash(file_bytes: bytes, model_name: str) -> str:
    """
    Replicate the exact algorithm used in cricket_knowledge._content_hash().

    Keeping this helper local means the tests remain valid contracts even if
    the internal implementation moves — the assertion is "same algorithm",
    and we verify it against the live function in some tests.
    """
    h = hashlib.sha256()
    h.update(file_bytes)
    h.update(model_name.encode())
    return h.hexdigest()


def _make_examples_hash(examples: list, model_name: str) -> str:
    """
    Replicate the hashing logic from _get_few_shot_selector() in prompts.py.

    Same two-update pattern: serialised examples + model name.
    """
    h = hashlib.sha256()
    h.update(json.dumps(examples, sort_keys=True).encode())
    h.update(model_name.encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """
    pydantic-settings caches the Settings object via @lru_cache.
    Clear the cache before and after every test so environment-variable
    patches take effect immediately.
    """
    # Import lazily so conftest sys.path injection runs first.
    from app.config import get_settings  # noqa: PLC0415
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def _patched_cricket_module():
    """
    Return the cricket_knowledge module with _RULES_PATH.read_bytes mocked to
    return fixed bytes and settings patched so no real disk I/O occurs.

    Yields (module, fixed_bytes, model_name).
    """
    import app.cricket_knowledge as ck  # noqa: PLC0415

    fixed_bytes = b"## Batting Rules\nSome rules here."
    model_name = "text-embedding-3-small"

    with patch.object(ck, "settings") as mock_settings, \
         patch.object(ck._RULES_PATH, "read_bytes", return_value=fixed_bytes):
        mock_settings.openai_embedding_model = model_name
        yield ck, fixed_bytes, model_name


# ---------------------------------------------------------------------------
# TestContentHash
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestContentHash:
    """Tests for cricket_knowledge._content_hash()."""

    def test_content_hash_same_file_same_model_is_deterministic(self):
        """Calling _content_hash() twice with identical inputs must return
        the same hex digest (SHA-256 is deterministic)."""
        import app.cricket_knowledge as ck

        fixed_bytes = b"## Bowling Rules\nEconomy = runs / overs."
        model = "text-embedding-3-small"

        with patch.object(ck, "settings") as mock_settings, \
             patch.object(ck, "_RULES_PATH") as mock_path:
            mock_settings.openai_embedding_model = model
            mock_path.read_bytes.return_value = fixed_bytes

            first = ck._content_hash()
            second = ck._content_hash()

        assert first == second, (
            "_content_hash() is not deterministic — same inputs produced "
            "different digests on consecutive calls."
        )

    def test_content_hash_matches_expected_algorithm(self):
        """The digest produced by _content_hash() must equal the value
        computed by the reference helper that mirrors the documented algorithm."""
        import app.cricket_knowledge as ck

        fixed_bytes = b"## Fielding Rules\nCatches win matches."
        model = "text-embedding-3-small"

        with patch.object(ck, "settings") as mock_settings, \
             patch.object(ck, "_RULES_PATH") as mock_path:
            mock_settings.openai_embedding_model = model
            mock_path.read_bytes.return_value = fixed_bytes

            actual = ck._content_hash()

        expected = _make_content_hash(fixed_bytes, model)
        assert actual == expected

    def test_content_hash_changes_on_file_change(self):
        """Different file bytes with the same model must produce a different hash."""
        import app.cricket_knowledge as ck

        model = "text-embedding-3-small"
        bytes_v1 = b"## Batting Rules\nOriginal content."
        bytes_v2 = b"## Batting Rules\nUpdated content - rule changed."

        with patch.object(ck, "settings") as mock_settings, \
             patch.object(ck, "_RULES_PATH") as mock_path:
            mock_settings.openai_embedding_model = model

            mock_path.read_bytes.return_value = bytes_v1
            hash_v1 = ck._content_hash()

            mock_path.read_bytes.return_value = bytes_v2
            hash_v2 = ck._content_hash()

        assert hash_v1 != hash_v2, (
            "_content_hash() returned the same digest for different file contents."
        )

    def test_content_hash_changes_on_model_change(self):
        """Same file bytes with a different embedding model must produce a
        different hash — this is the core of the embedding-versioning feature."""
        import app.cricket_knowledge as ck

        fixed_bytes = b"## Powerplay Rules\nFirst six overs."
        model_a = "text-embedding-3-small"
        model_b = "text-embedding-ada-002"

        with patch.object(ck, "_RULES_PATH") as mock_path:
            mock_path.read_bytes.return_value = fixed_bytes

            with patch.object(ck, "settings") as mock_settings:
                mock_settings.openai_embedding_model = model_a
                hash_a = ck._content_hash()

            with patch.object(ck, "settings") as mock_settings:
                mock_settings.openai_embedding_model = model_b
                hash_b = ck._content_hash()

        assert hash_a != hash_b, (
            "_content_hash() returned the same digest for different embedding models. "
            "Switching models would silently serve stale vectors — the versioning "
            "feature is broken."
        )

    def test_content_hash_includes_model_name(self):
        """Verify that the model name is actually part of the digest by computing
        a hash with and without the model suffix and confirming they differ.

        This test guards against a regression where the second h.update() call
        (for the model name) is accidentally removed."""
        import app.cricket_knowledge as ck

        fixed_bytes = b"Some rules content."
        model = "text-embedding-3-small"

        # Hash produced by the real function (includes model name)
        with patch.object(ck, "settings") as mock_settings, \
             patch.object(ck, "_RULES_PATH") as mock_path:
            mock_settings.openai_embedding_model = model
            mock_path.read_bytes.return_value = fixed_bytes
            hash_with_model = ck._content_hash()

        # Hash produced if ONLY the file bytes were hashed (no model name)
        hash_file_only = hashlib.sha256(fixed_bytes).hexdigest()

        assert hash_with_model != hash_file_only, (
            "_content_hash() appears NOT to include the model name — its digest "
            "matches a hash of the file bytes alone."
        )

    @pytest.mark.parametrize("model_a,model_b", [
        ("text-embedding-3-small", "text-embedding-ada-002"),
        ("text-embedding-3-small", "text-embedding-3-large"),
        ("text-embedding-ada-002",  "text-embedding-3-large"),
    ])
    def test_content_hash_all_model_pairs_differ(self, model_a, model_b):
        """Any pair of distinct model names must produce distinct hashes when
        the file content is held constant."""
        import app.cricket_knowledge as ck

        fixed_bytes = b"## DRS Rules\nTwo reviews per innings."

        with patch.object(ck, "_RULES_PATH") as mock_path:
            mock_path.read_bytes.return_value = fixed_bytes

            with patch.object(ck, "settings") as mock_settings:
                mock_settings.openai_embedding_model = model_a
                hash_a = ck._content_hash()

            with patch.object(ck, "settings") as mock_settings:
                mock_settings.openai_embedding_model = model_b
                hash_b = ck._content_hash()

        assert hash_a != hash_b, (
            f"Model pair ({model_a!r}, {model_b!r}) produced the same hash."
        )

    def test_content_hash_returns_hex_string(self):
        """_content_hash() must return a lowercase hex string of length 64
        (SHA-256 produces 32 bytes → 64 hex chars)."""
        import app.cricket_knowledge as ck

        with patch.object(ck, "settings") as mock_settings, \
             patch.object(ck, "_RULES_PATH") as mock_path:
            mock_settings.openai_embedding_model = "text-embedding-3-small"
            mock_path.read_bytes.return_value = b"content"
            digest = ck._content_hash()

        assert isinstance(digest, str)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# TestFewShotExamplesHash
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFewShotExamplesHash:
    """
    Tests for the hashing logic inside _get_few_shot_selector() in prompts.py.

    Rather than calling _get_few_shot_selector() (which touches ChromaDB and
    OpenAI), we replicate the exact hash algorithm and verify it against
    IPL_EXAMPLES.  This keeps tests fast and dependency-free while still
    asserting the contract: same examples + different model → different hash.
    """

    @pytest.fixture()
    def ipl_examples(self):
        """Return the real IPL_EXAMPLES list from prompts.py."""
        # Importing prompts triggers no side-effects if we avoid calling
        # _build_few_shot_prompt() — the module only defines constants and
        # functions at import time.
        import app.prompts as p  # noqa: PLC0415
        return p.IPL_EXAMPLES

    def test_examples_hash_same_examples_same_model_is_deterministic(self, ipl_examples):
        """Same examples + same model → identical hash on repeated calls."""
        model = "text-embedding-3-small"
        h1 = _make_examples_hash(ipl_examples, model)
        h2 = _make_examples_hash(ipl_examples, model)
        assert h1 == h2

    def test_examples_hash_includes_model_name(self, ipl_examples):
        """The examples hash must differ from a hash of examples alone.

        Confirms that the model name is actually contributed to the digest,
        mirroring the test_content_hash_includes_model_name guard above."""
        model = "text-embedding-3-small"
        hash_with_model = _make_examples_hash(ipl_examples, model)
        hash_examples_only = hashlib.sha256(
            json.dumps(ipl_examples, sort_keys=True).encode()
        ).hexdigest()
        assert hash_with_model != hash_examples_only

    def test_examples_hash_changes_on_model_change(self, ipl_examples):
        """Same IPL_EXAMPLES, different model → different hash."""
        hash_small = _make_examples_hash(ipl_examples, "text-embedding-3-small")
        hash_ada = _make_examples_hash(ipl_examples, "text-embedding-ada-002")
        assert hash_small != hash_ada, (
            "Few-shot examples hash did not change when the embedding model changed. "
            "A model upgrade would silently reuse stale vectors."
        )

    def test_examples_hash_changes_on_examples_change(self, ipl_examples):
        """Modified examples list + same model → different hash."""
        model = "text-embedding-3-small"
        modified_examples = ipl_examples + [{"input": "Extra example", "query": "SELECT 1"}]
        original_hash = _make_examples_hash(ipl_examples, model)
        modified_hash = _make_examples_hash(modified_examples, model)
        assert original_hash != modified_hash

    @pytest.mark.parametrize("model_a,model_b", [
        ("text-embedding-3-small", "text-embedding-ada-002"),
        ("text-embedding-3-small", "text-embedding-3-large"),
        ("text-embedding-ada-002",  "text-embedding-3-large"),
    ])
    def test_examples_hash_all_model_pairs_differ(self, ipl_examples, model_a, model_b):
        """Any two distinct model names produce distinct hashes for the same examples."""
        hash_a = _make_examples_hash(ipl_examples, model_a)
        hash_b = _make_examples_hash(ipl_examples, model_b)
        assert hash_a != hash_b

    def test_examples_hash_is_sort_keys_stable(self):
        """Hash must be stable regardless of Python dict key insertion order.

        json.dumps(..., sort_keys=True) is used precisely to guarantee this.
        Two dicts with identical key-value pairs but different insertion orders
        must hash identically."""
        model = "text-embedding-3-small"
        example_abc = {"input": "Some question?", "query": "SELECT 1"}
        example_bca = {"query": "SELECT 1", "input": "Some question?"}
        h_abc = _make_examples_hash([example_abc], model)
        h_bca = _make_examples_hash([example_bca], model)
        assert h_abc == h_bca, (
            "Hash is not stable under dict key ordering — sort_keys=True may be missing."
        )


# ---------------------------------------------------------------------------
# TestSettingsEmbeddingModel
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSettingsEmbeddingModel:
    """Tests for the openai_embedding_model field in config.Settings."""

    def test_settings_embedding_model_default(self):
        """get_settings().openai_embedding_model must default to
        'text-embedding-3-small' when the env var is not set."""
        # Provide required fields; patch away env file so the test is hermetic.
        env_overrides = {
            "OPENAI_API_KEY": "sk-test",
            "DB_USER": "user",
            "DB_PASSWORD": "pass",
            "DB_HOST": "localhost",
            "DB_NAME": "ipl_db",
        }
        # Patch env and disable .env file loading by pointing at a non-existent path.
        with patch.dict(os.environ, env_overrides, clear=False), \
             patch("app.config.Settings.model_config",
                   {"env_file": "/nonexistent/.env",
                    "env_file_encoding": "utf-8",
                    "case_sensitive": False,
                    "extra": "ignore"}):
            from app.config import Settings  # noqa: PLC0415
            s = Settings(
                openai_api_key="sk-test",
                db_user="user",
                db_password="pass",
                db_host="localhost",
                db_name="ipl_db",
            )
        assert s.openai_embedding_model == "text-embedding-3-small"

    def test_settings_embedding_model_from_env(self):
        """When OPENAI_EMBEDDING_MODEL is set in the environment,
        Settings() must pick it up and expose it as openai_embedding_model."""
        env_overrides = {
            "OPENAI_API_KEY": "sk-test",
            "DB_USER": "user",
            "DB_PASSWORD": "pass",
            "DB_HOST": "localhost",
            "DB_NAME": "ipl_db",
            "OPENAI_EMBEDDING_MODEL": "text-embedding-ada-002",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            from app.config import Settings  # noqa: PLC0415
            s = Settings(
                openai_api_key="sk-test",
                db_user="user",
                db_password="pass",
                db_host="localhost",
                db_name="ipl_db",
            )
        assert s.openai_embedding_model == "text-embedding-ada-002"

    def test_settings_embedding_model_direct_kwarg(self):
        """The field must accept an explicit kwarg on construction,
        overriding the default without any environment variables."""
        from app.config import Settings  # noqa: PLC0415
        s = Settings(
            openai_api_key="sk-test",
            db_user="user",
            db_password="pass",
            db_host="localhost",
            db_name="ipl_db",
            openai_embedding_model="text-embedding-3-large",
        )
        assert s.openai_embedding_model == "text-embedding-3-large"

    def test_settings_embedding_model_is_string(self):
        """openai_embedding_model must be a plain str, not None or another type."""
        from app.config import Settings  # noqa: PLC0415
        s = Settings(
            openai_api_key="sk-test",
            db_user="user",
            db_password="pass",
            db_host="localhost",
            db_name="ipl_db",
        )
        assert isinstance(s.openai_embedding_model, str)
        assert len(s.openai_embedding_model) > 0
