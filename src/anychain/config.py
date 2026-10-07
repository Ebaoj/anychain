"""Configuration: one validated YAML file describes the whole network."""
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


DEFAULT_CONFIG_ENV = "ANYCHAIN_CONFIG"


def _http_url(value: str) -> str:
    """Accept only http(s) URLs, so an unfilled '<PLACEHOLDER>' fails at load time."""
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def _template(value: str, **fields: str) -> str:
    """Check a link template only uses the placeholders we fill in."""
    try:
        value.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"template {value!r} may only use {{{'}, {'.join(fields)}}}: {exc}") from None
    return value


class NetworkConfig(BaseModel):
    name: str
    chain_id: int = Field(ge=0)
    native_symbol: str
    native_decimals: int = 18
    # Same values as Blockscout's CHAIN_TYPE; selects the profile in chains.py.
    chain_type: str = "default"

    @field_validator("chain_type")
    @classmethod
    def _chain_type_text(cls, v: str) -> str:
        # Unknown values (older Blockscout versions, new chain types) are accepted: the
        # generic profile is used and every answer says so. Only an empty value is an error.
        if not v.strip():
            raise ValueError("chain_type cannot be empty; use 'default' for a generic EVM")
        return v.strip()


class ExplorerConfig(BaseModel):
    type: str = "blockscout"
    base_url: str
    api_path: str
    tx_url_template: str
    address_url_template: str
    timeout_s: float = 15

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _http_url(v)

    @field_validator("tx_url_template")
    @classmethod
    def _tx_template(cls, v: str) -> str:
        return _template(v, base_url="https://x", hash="0x1")

    @field_validator("address_url_template")
    @classmethod
    def _address_template(cls, v: str) -> str:
        return _template(v, base_url="https://x", address="0x1")

    @field_validator("type")
    @classmethod
    def _only_blockscout(cls, v: str) -> str:
        if v != "blockscout":
            raise ValueError("only 'blockscout' explorers are supported")
        return v

    @property
    def api_base(self) -> str:
        return f"{self.base_url}{self.api_path}"

    def tx_url(self, tx_hash: str) -> str:
        return self.tx_url_template.format(base_url=self.base_url, hash=tx_hash)


class RpcConfig(BaseModel):
    url: str
    timeout_s: float = 10
    supports_debug_trace: bool = False  # used from phase 2

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _http_url(v)


class RepoConfig(BaseModel):
    url: str
    ref: str = "main"
    source_globs: list[str] = ["contracts/**/*.sol"]
    artifact_globs: list[str] = []


class SignatureDbConfig(BaseModel):
    enabled: bool = False
    url: str | None = None


class AbiStrategyConfig(BaseModel):
    order: list[str] = ["explorer", "repo_artifacts", "repo_source_signatures", "signature_db"]
    signature_db: SignatureDbConfig = SignatureDbConfig()

    @field_validator("order")
    @classmethod
    def _known_sources(cls, v: list[str]) -> list[str]:
        known = {"explorer", "repo_artifacts", "repo_source_signatures", "signature_db"}
        unknown = set(v) - known
        if unknown:
            raise ValueError(f"unknown ABI sources: {sorted(unknown)}")
        return v


class LlmConfig(BaseModel):
    provider: str = "anthropic"
    model: str
    max_tokens: int = 2000
    temperature: float = 0


class AssistantConfig(BaseModel):
    default_mode: str = "support"
    language: str = "en"
    max_clarifying_questions: int = 2
    time_budget_s: float = Field(default=30, gt=0)  # max total wait for one explanation

    @field_validator("default_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"support", "developer", "auditor"}:
            raise ValueError("mode must be support, developer or auditor")
        return v

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        if v not in {"pt-BR", "en"}:
            raise ValueError("language must be pt-BR or en")
        return v


class StorageConfig(BaseModel):
    sqlite_path: str = "data/runs.db"
    cache_dir: str = "data/cache"


# Sections accepted now and used from phase 2 on: repos, address_map,
# abi_strategy (beyond "explorer"), storage. They are validated already, so a
# typo is caught today rather than when the feature arrives.
class AppConfig(BaseModel):
    network: NetworkConfig
    explorer: ExplorerConfig
    rpc: RpcConfig
    repos: list[RepoConfig] = []
    address_map: dict[str, dict[str, str]] = {}
    # Names for special addresses of this network (system contracts, bridges), shown when
    # the explorer has no name for them. Network knowledge lives here, not in code.
    address_labels: dict[str, str] = {}

    @field_validator("address_labels")
    @classmethod
    def _label_keys_are_addresses(cls, v: dict[str, str]) -> dict[str, str]:
        bad = [k for k in v if not (isinstance(k, str) and k.startswith("0x") and len(k) == 42)]
        if bad:
            raise ValueError(f"address_labels keys must be 0x-prefixed 20-byte addresses: {bad}")
        return {k.lower(): label for k, label in v.items()}
    abi_strategy: AbiStrategyConfig = AbiStrategyConfig()
    llm: LlmConfig
    assistant: AssistantConfig = AssistantConfig()
    storage: StorageConfig = StorageConfig()


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


def load_config(path: str | None = None) -> AppConfig:
    """Load the config from `path`, or from $ANYCHAIN_CONFIG."""
    chosen = path or os.environ.get(DEFAULT_CONFIG_ENV)
    if not chosen:
        raise ConfigError(f"No config given. Use --config or set ${DEFAULT_CONFIG_ENV}.")
    file = Path(chosen)
    if not file.is_file():
        raise ConfigError(f"Config file not found: {file}")
    try:
        raw = yaml.safe_load(file.read_text())
        return AppConfig.model_validate(raw)
    except Exception as exc:  # yaml or pydantic errors: show them plainly
        raise ConfigError(f"Invalid config {file}: {exc}") from exc
