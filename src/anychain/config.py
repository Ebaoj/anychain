"""Configuration: one validated YAML file describes the whole network."""
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Contract that also records native-currency movements as token transfers (zkSync's
    # L2BaseToken, Celo's CELO token). Its transfers are the native movement, not a second asset.
    native_token_contract: str | None = None
    # Address that collects fees through visible transfers (zkSync's bootloader).
    fee_collector: str | None = None
    # Highest system-contract address (zkSync: 0xffff, its kernel space). Internal calls between
    # two system contracts are grouped into one line so the user's own calls stay visible.
    system_address_max: int | None = None

    @field_validator("system_address_max", mode="before")
    @classmethod
    def _hex_or_int(cls, v: object) -> object:
        return int(v, 16) if isinstance(v, str) and v.startswith("0x") else v

    @field_validator("native_token_contract", "fee_collector")
    @classmethod
    def _optional_address(cls, v: str | None) -> str | None:
        if v is not None and not (v.startswith("0x") and len(v) == 42):
            raise ValueError(f"must be a 0x-prefixed 20-byte address, got {v!r}")
        return v.lower() if v else v

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


class FeeTokenConfig(BaseModel):
    """How to read a fee paid through an address the explorer cannot describe (e.g. a Celo fee adapter)."""

    symbol: str
    decimals: int = Field(ge=0)  # decimals of the fee amount (the adapter's units, not the token's)
    note: str | None = None


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


class AddressMapEntry(BaseModel):
    repo: str  # one of the configured repos' url
    contract: str  # the contract's name in that repo


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
    provider: Literal["claude_code", "anthropic", "openai"] = "claude_code"  # D27
    model: str
    max_tokens: int = 2000  # API providers only
    temperature: float | None = 0  # API providers only; null = the model's default (OpenAI reasoning models)
    timeout_s: float = 120
    claude_code_command: str = "claude"


class AssistantConfig(BaseModel):
    default_mode: str = "support"
    language: str = "en"
    max_clarifying_questions: int = 1  # triage asks one question at most (PHASE4 D2); 0 turns it off
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
    sqlite_path: str = "data/runs.db"  # event log (one row per answer; `anychain log` reads it)
    cache_dir: str = "data/cache"
    event_sink: Literal["sqlite", "none"] = "sqlite"  # production would add a queue sink (D24)


class CacheConfig(BaseModel):
    """Answers kept by (network, hash) when their facts cannot change (PHASE3 T0, D44)."""
    enabled: bool = True
    path: str = "data/cache.db"
    # Without the node's "finalized" block (Rootstock rejects the tag), a block this deep counts as final.
    min_confirmations: int = Field(64, ge=1)
    # Even a final answer is fetched again after this: explorer names, labels and verification can change.
    max_age_days: float = Field(30, gt=0)


# Sections accepted now and used from phase 2 on: repos, address_map,
# abi_strategy (beyond "explorer"), storage. They are validated already, so a
# typo is caught today rather than when the feature arrives.
class AppConfig(BaseModel):
    network: NetworkConfig
    explorer: ExplorerConfig
    rpc: RpcConfig
    repos: list[RepoConfig] = []
    # Contracts whose code is in a configured repo, by address: {"0x...": {"repo": <repos[].url>, "contract": Name}}.
    # Used to decode a contract the explorer has no ABI for (a private network's, for example).
    address_map: dict[str, AddressMapEntry] = {}
    # Names for special addresses of this network (system contracts, bridges), shown when
    # the explorer has no name for them. Network knowledge lives here, not in code.
    address_labels: dict[str, str] = {}

    # Contracts built into the node (no bytecode or ABI to verify), e.g. Rootstock's Bridge.
    native_contracts: dict[str, str] = {}
    # Fee tokens the explorer cannot describe, keyed by the address in the fee (Celo adapters).
    fee_tokens: dict[str, FeeTokenConfig] = {}

    @field_validator("address_labels", "native_contracts", "fee_tokens", "address_map")
    @classmethod
    def _keys_are_addresses(cls, v: dict) -> dict:
        bad = [k for k in v if not (isinstance(k, str) and k.startswith("0x") and len(k) == 42)]
        if bad:
            raise ValueError(f"keys must be 0x-prefixed 20-byte addresses: {bad}")
        return {k.lower(): value for k, value in v.items()}

    @model_validator(mode="after")
    def _mapped_repos_are_configured(self) -> "AppConfig":
        urls = {r.url for r in self.repos}
        unknown = sorted({e.repo for e in self.address_map.values()} - urls)
        if unknown:
            raise ValueError(f"address_map names repos that are not in repos: {unknown}")
        return self

    def label_for(self, address: str) -> str | None:
        """Config name for an address: a native contract or a labelled one."""
        key = address.lower()
        if key in self.native_contracts:
            return f"{self.native_contracts[key]}, native contract"
        return self.address_labels.get(key)
    abi_strategy: AbiStrategyConfig = AbiStrategyConfig()
    llm: LlmConfig
    assistant: AssistantConfig = AssistantConfig()
    cache: CacheConfig = CacheConfig()
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
