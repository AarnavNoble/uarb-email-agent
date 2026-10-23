import pytest

from uarb_agent import config

# Keep the developer's real .env (API key, Gmail app password) out of every test: an env var that is set,
# even to "", wins over .env because load_dotenv never overrides.
_ISOLATE = {"LLM_ENABLED": "false", "ANTHROPIC_API_KEY": "", "AGENT_EMAIL": "", "AGENT_APP_PASSWORD": ""}


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    for name, value in _ISOLATE.items():
        monkeypatch.setenv(name, value)
    cache_clear = getattr(config.get_settings, "cache_clear", None)
    if cache_clear:
        cache_clear()
    yield
    if cache_clear:
        cache_clear()
