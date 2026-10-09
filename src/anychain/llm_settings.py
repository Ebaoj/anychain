"""The model a reader picks and its API key, saved from the CLI (`anychain llm set`) or the page (D61).

Stored in one file outside the repository, readable by its owner only (0600, in a 0700 folder):
$ANYCHAIN_LLM_SETTINGS, default ~/.config/anychain/llm.json. The key is never returned: `describe()` gives only
its last four characters. The saved provider and model take the place of the config's `llm` block; the saved key
comes before the provider's environment variable (ANTHROPIC_API_KEY, OPENAI_API_KEY).
"""
import json
import os
from pathlib import Path

from anychain.config import LlmConfig

PROVIDERS = ("claude_code", "anthropic", "openai")
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5"}  # OpenAI: the reader names the model


def path() -> Path:
    return Path(os.environ.get("ANYCHAIN_LLM_SETTINGS", "~/.config/anychain/llm.json")).expanduser()


def load() -> dict:
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(provider: str, model: str | None = None, api_key: str | None = None) -> dict:
    """Saves the choice (and the key, when given; a key saved earlier for that provider is kept otherwise)."""
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    model = (model or "").strip() or DEFAULT_MODELS.get(provider)
    if provider == "openai" and not model:
        raise ValueError("name the OpenAI model to use")
    data = load()
    data["provider"], data["model"] = provider, model
    keys = data.setdefault("keys", {})
    if api_key and api_key.strip() and provider in ENV_KEYS:
        keys[provider] = api_key.strip()
    _write(data)
    return describe()


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass


def describe() -> dict:
    """What is saved, without any key: the provider, the model and, per provider, whether a key is there."""
    data = load()
    keys = data.get("keys") or {}
    return {"provider": data.get("provider"), "model": data.get("model"),
            "keys": {p: (f"saved, ends in {keys[p][-4:]}" if keys.get(p) else
                         ("from the environment" if os.environ.get(env) else None)) for p, env in ENV_KEYS.items()}}


def key_for(provider: str) -> str | None:
    saved = (load().get("keys") or {}).get(provider)
    return saved or os.environ.get(ENV_KEYS.get(provider, ""), None) or None


def effective(llm: LlmConfig) -> LlmConfig:
    """The config's model, or the one the reader saved."""
    data = load()
    if data.get("provider") not in PROVIDERS:
        return llm
    return llm.model_copy(update={"provider": data["provider"], "model": data.get("model") or llm.model})


def _write(data: dict) -> None:
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)
