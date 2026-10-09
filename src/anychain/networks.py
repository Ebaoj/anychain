"""Switching the network from the page (D64): one app per network config, built when first used, and an ASGI
dispatcher that sends each request to the active one. A network is still only its config file."""
from pathlib import Path

from anychain.config import AppConfig, ConfigError, load_config


def available(config_dir: Path) -> dict[str, Path]:
    """{network name: config path} for every config in the folder that loads (templates such as
    cloudwalk.example.yaml are left out: they have placeholders to fill)."""
    out: dict[str, Path] = {}
    for path in sorted(config_dir.glob("*.yaml")):
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
