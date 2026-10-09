"""Switching the network from the page (D64): one app per network config, built when first used, and an ASGI
dispatcher that sends each request to the active one. A network is still only its config file."""
import os
import re
from pathlib import Path

import yaml

from anychain.config import AppConfig, ConfigError, load_config

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def user_dir() -> Path:
    """Networks added on the page, outside the repository (D65): $ANYCHAIN_NETWORKS_DIR or
    ~/.config/anychain/networks, readable by their owner only (an RPC URL can carry a provider's key)."""
    return Path(os.environ.get("ANYCHAIN_NETWORKS_DIR", "~/.config/anychain/networks")).expanduser()


def available(config_dir: Path) -> dict[str, Path]:
    """{network name: config path} for every config that loads, the shipped ones and the reader's own (templates
    such as cloudwalk.example.yaml are left out: they have placeholders to fill)."""
    out: dict[str, Path] = {}
    folders = [config_dir] + ([user_dir()] if user_dir().is_dir() else [])
    for path in sorted(p for folder in folders for p in sorted(folder.glob("*.yaml"))):
        if ".example." in path.name:
            continue
        try:
            out.setdefault(load_config(str(path)).network.name, path)
        except ConfigError:
            continue
    return out


class NetworkSwitcher:
    def __init__(self, paths: dict[str, Path], active: AppConfig, make_app):
        self.paths, self.make_app = dict(paths), make_app
        self.active = active.network.name
        self.configs: dict[str, AppConfig] = {self.active: active}
        self.apps: dict[str, object] = {}

    def networks(self) -> list[dict]:
        out = []
        for name in sorted(set(self.paths) | {self.active}):
            cfg = self._config(name)
            out.append({"name": name, "chain_id": cfg.network.chain_id, "explorer": cfg.explorer.base_url,
                        "active": name == self.active})
        return out

    def add(self, name: str, path: Path) -> None:
        self.paths[name] = path
        self.configs.pop(name, None)
        self.apps.pop(name, None)  # an edited network is built again from its file

    def select(self, name: str) -> None:
        if name not in self.paths and name != self.active:
            raise KeyError(name)
        self._config(name)  # a broken config is refused before it becomes the active one
        self.active = name

    def _config(self, name: str) -> AppConfig:
        if name not in self.configs:
            self.configs[name] = load_config(str(self.paths[name]))
        return self.configs[name]

    def _app(self):
        if self.active not in self.apps:
            self.apps[self.active] = self.make_app(self._config(self.active), self)
        return self.apps[self.active]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":  # the per-network apps need no startup work
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await self._app()(scope, receive, send)


def draft_config(draft: dict, base: AppConfig) -> AppConfig:
    """A network from the page's form (D65): its own name, chain, explorer and node, everything else (model,
    language, storage) from the network in use. Validated like any config file."""
    name = str(draft.get("name") or "").strip().lower()
    if not NAME.match(name):
        raise ConfigError("the name takes lowercase letters, digits and dashes (2 to 41 characters)")
    data = base.model_dump(mode="json")
    data.update({
        "network": {"name": name, "chain_id": draft.get("chain_id"), "native_symbol": draft.get("native_symbol"),
                    "native_decimals": draft.get("native_decimals", 18), "chain_type": draft.get("chain_type") or "default"},
        "explorer": {**data["explorer"], "base_url": str(draft.get("explorer_url") or "").rstrip("/")},
        "rpc": {**data["rpc"], "url": str(draft.get("rpc_url") or "")},
        "repos": [], "address_map": {}, "address_labels": {}, "native_contracts": {}, "fee_tokens": {}, "examples": [],
    })
    try:
        cfg = AppConfig.model_validate(data)
    except Exception as exc:  # pydantic: say which field
        raise ConfigError(f"invalid network: {exc}") from exc
    for url in (cfg.explorer.base_url, cfg.rpc.url):
        if not re.match(r"^https?://[^\s/]+", url):
            raise ConfigError(f"not an http(s) address: {url!r}")
    return cfg


def save_user_network(cfg: AppConfig) -> Path:
    """Writes the network as a config file in the reader's folder, readable by its owner only."""
    folder = user_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{cfg.network.name}.yaml"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("# Added on the page (D65). Edit it here or on the page; everything a network needs is in this file.\n")
        yaml.safe_dump(cfg.model_dump(mode="json", exclude_defaults=True), out, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    return path
