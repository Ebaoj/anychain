"""The model a reader picks and its API key, saved from the CLI (`anychain llm set`) or the page (D61).

Stored in one file outside the repository, readable by its owner only (0600, in a 0700 folder):
$ANYCHAIN_LLM_SETTINGS, default ~/.config/anychain/llm.json. The key is never returned: `describe()` gives only
its last four characters. The saved provider and model take the place of the config's `llm` block; the saved key
comes before the provider's environment variable (ANTHROPIC_API_KEY, OPENAI_API_KEY).
"""
import json
import os
import re
from pathlib import Path

import httpx

from anychain.config import LlmConfig

PROVIDERS = ("claude_code", "anthropic", "openai")
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
# The providers' own model lists, so a reader picks a model the key can use instead of typing a name:
#   Anthropic GET /v1/models (x-api-key, anthropic-version 2023-06-01; data[].id, display_name, lifecycle),
#   checked in its API reference on 2026-10-09; OpenAI GET /v1/models (Bearer; data[].id, created), as the
#   official openai-python SDK reads it (resources/models.py, types/model.py), checked the same day.
MODELS_URL = {"anthropic": "https://api.anthropic.com/v1/models", "openai": "https://api.openai.com/v1/models"}
# What the evaluation found (D76): gpt-4.1-mini is the smallest OpenAI model that answered the chat's open questions
# right; gpt-4.1-nano misread a fact it cited correctly, twice. Shown next to the model in the page and `llm set`.
ADVICE = {"gpt-4.1-mini": "recommended minimum", "gpt-4.1-nano": "not recommended: misreads facts on open questions"}
# OpenAI lists every model of the account; these families do not write chat answers.
NOT_CHAT = re.compile(r"embedding|whisper|tts|dall-e|moderation|davinci|babbage|transcribe|image|audio|realtime|"
                      r"search|sora", re.IGNORECASE)


class ModelListError(Exception):
    """The provider's model list could not be read (no key, refused, unreachable). Never carries the key."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=15)


def list_models(provider: str, api_key: str | None = None) -> list[dict]:
    """The models this key can use to write answers, newest first: [{"id", "label"}]."""
    if provider not in MODELS_URL:
        raise ModelListError(f"{provider} has no model list here")
    key = (api_key or "").strip() or key_for(provider)
    if not key:
        raise ModelListError(f"no {provider} API key: paste it first")
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if provider == "anthropic"
               else {"Authorization": f"Bearer {key}"})
    try:
        with _client() as client:
            response = client.get(MODELS_URL[provider], headers=headers,
                                  params={"limit": 1000} if provider == "anthropic" else None)
    except httpx.HTTPError as exc:
        raise ModelListError(f"{provider} did not answer ({type(exc).__name__})") from exc
    if response.status_code in (401, 403):
        raise ModelListError(f"{provider} refused the key (HTTP {response.status_code}): check it")
    if response.status_code != 200:  # the body is not shown: a provider's text can echo part of a key
        raise ModelListError(f"{provider} answered HTTP {response.status_code}")
    try:
        data = response.json().get("data") or []
    except ValueError as exc:
        raise ModelListError(f"{provider} sent an unreadable list") from exc
    if provider == "anthropic":
        return [{"id": m["id"], "label": (m.get("display_name") or m["id"])
                 + (" (deprecated)" if m.get("lifecycle") == "deprecated" else "")}
                for m in data if isinstance(m, dict) and m.get("id") and m.get("lifecycle") != "retired"]
    chat = [m for m in data if isinstance(m, dict) and m.get("id") and not NOT_CHAT.search(m["id"])]
    chat.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return [{"id": m["id"], "label": m["id"] + (f" ({ADVICE[m['id']]})" if m["id"] in ADVICE else "")} for m in chat]


def path() -> Path:
    return Path(os.environ.get("ANYCHAIN_LLM_SETTINGS", "~/.config/anychain/llm.json")).expanduser()


def load() -> dict:
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(provider: str, model: str | None = None, api_key: str | None = None) -> dict:
    """Saves the choice (and the key, when given; a key saved earlier for that provider is kept otherwise). For an
    API provider the model must be one the provider lists for this key (no typed names: no typos)."""
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
    model = (model or "").strip() or None
    if provider in MODELS_URL:
        if not model:
            raise ValueError(f"pick the {provider} model from the list")
        try:
            ids = {m["id"] for m in list_models(provider, api_key)}
        except ModelListError as exc:
            raise ValueError(f"the model could not be checked: {exc}") from exc
        if model not in ids:
            raise ValueError(f"{model!r} is not in the models {provider} lists for this key")
    data = load()
    data["provider"], data["model"] = provider, model
    keys = data.setdefault("keys", {})
    if api_key and api_key.strip() and provider in ENV_KEYS:
        keys[provider] = api_key.strip()
    _write(data)
    return describe()


LANGUAGES = ("pt-BR", "en", "es")


def save_language(language: str) -> None:
    """The language answers are written in, chosen on the page (D64); the config's applies until then."""
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
    data = load()
    data["language"] = language
    _write(data)


def language(default: str) -> str:
    chosen = load().get("language")
    return chosen if chosen in LANGUAGES else default


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
