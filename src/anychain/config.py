"""Configuration: one validated YAML file describes the whole network."""
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_ENV = "ANYCHAIN_CONFIG"


class NetworkConfig(BaseModel):
    name: str
    chain_id: int = Field(ge=0)
    native_symbol: str
    native_decimals: int = 18


class ExplorerConfig(BaseModel):
    type: str = "blockscout"
    base_url: str
    api_path: str = "/api/v2"
    tx_url_template: str = "{base_url}/tx/{hash}"
    address_url_template: str = "{base_url}/address/{address}"
    timeout_s: float = 15

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

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

    def address_url(self, address: str) -> str:
        return self.address_url_template.format(base_url=self.base_url, address=address)


class RpcConfig(BaseModel):
    url: str
    timeout_s: float = 10
    supports_debug_trace: bool = False


class RepoConfig(BaseModel):
    url: str
    ref: str = "main"
    source_globs: list[str] = ["contracts/**/*.sol"]
    artifact_globs: list[str] = []


class SignatureDbConfig(BaseModel):
    enabled: bool = True
    url: str = "https://www.4byte.directory/api/v1"


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


class AppConfig(BaseModel):
    network: NetworkConfig
    explorer: ExplorerConfig
    rpc: RpcConfig
    repos: list[RepoConfig] = []
    address_map: dict[str, dict[str, str]] = {}
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
