"""Configured contract repositories, kept in a local cache (PHASE2 T6, R4).

`anychain repos sync` downloads each configured repo at its commit into storage.cache_dir/repos/;
`explain` only reads that cache, so answering stays fast and never depends on GitHub being up. A
branch `ref` is resolved to a commit once, at sync time, and that commit is what permalinks name.
Only GitHub repositories are supported (downloaded as the commit's tarball from codeload).
"""
import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from anychain.collectors.http import USER_AGENT, CollectorError
from anychain.config import RepoConfig
from anychain.solidity import SolidityIndex

GITHUB = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class ArtifactIndex:
    """Compiled ABIs in a repository (PHASE2_5 T3, R3, D40), by contract name.

    Shapes read: Hardhat and Truffle files (`{"contractName": …, "abi": [...]}`), Safe's deployment files
    (the same, plus `networkAddresses` and `deployments`), hardhat-deploy files (`{"address": …, "abi": …}`,
    the name is the file's), Foundry files (`out/X.sol/X.json` or `X.0.8.19.json`: the name is the file's,
    up to its first dot) and plain ABI lists (`abi/X.json`). Hardhat's `build-info/` and `.dbg.json` files
    are never read; files that are not JSON are skipped.

    A file may list the addresses it was deployed at (per chain, or one address); then a pin is checked
    against that list (`match`). A name with two different ABIs and no address to choose by is ambiguous.
    """

    def __init__(self, entries: list["Artifact"]):
        self.entries: dict[str, list[Artifact]] = {}
        for e in entries:
            self.entries.setdefault(e.name, []).append(e)

    @property
    def ambiguous(self) -> set[str]:
        return {name for name, found in self.entries.items() if len({_abi_key(e.abi) for e in found}) > 1}

    @classmethod
    def from_dir(cls, root: Path, globs: list[str]) -> "ArtifactIndex":
        key = (str(root), tuple(globs))
        if key not in _ARTIFACTS:
            _ARTIFACTS[key] = cls(_read_artifacts(root, globs))
        return _ARTIFACTS[key]

    def find(self, name: str) -> tuple[str, list] | None:
        """(path, ABI) of the contract `name` when its files agree on one ABI, else None."""
        found = self.entries.get(name) or []
        if not found or name in self.ambiguous:
            return None
        return found[0].path, found[0].abi

    def match(self, name: str, address: str, chain_id: int) -> "ArtifactMatch | str":
        """The ABI for a pin of `address` to `name`, or the reason there is none. When the files list where they
        were deployed, only a file listing this address on this chain is used (`listed`); one listing other
        addresses means the pin is wrong."""
        found = self.entries.get(name) or []
        if not found:
            return f"its compiled artifacts have no ABI named {name}"
        with_lists = [e for e in found if e.addresses is not None]
        if with_lists:
            here = [e for e in with_lists if address.lower() in e.addresses.get(chain_id, set()) | e.addresses.get(None, set())]
            if not here:
                listed = sorted({a for e in with_lists for a in e.addresses.get(chain_id, set()) | e.addresses.get(None, set())})
                return (f"its artifact for {name} lists " + (", ".join(_checksum(a) for a in listed) if listed else "no address")
                        + f" on chain {chain_id}, not this one")
            if len({_abi_key(e.abi) for e in here}) > 1:
                return f"its compiled artifacts have two different ABIs named {name} for this address"
            return ArtifactMatch(here[0].path, here[0].abi, listed=True)
        if name in self.ambiguous:
            return f"its compiled artifacts have two different ABIs named {name}"
        return ArtifactMatch(found[0].path, found[0].abi, listed=False)


@dataclass(frozen=True)
class Artifact:
    path: str
    name: str
    abi: list
    addresses: dict | None  # chain id (None: any chain) -> lowercase addresses; None when the file lists none


@dataclass(frozen=True)
class ArtifactMatch:
    path: str
    abi: list
    listed: bool  # the file itself lists the pinned address for this chain


_ARTIFACTS: dict[tuple, ArtifactIndex] = {}  # per (tree, globs): a synced commit never changes


def _read_artifacts(root: Path, globs: list[str]) -> list[Artifact]:
    entries = []
    for pattern in globs:
        for p in sorted(root.glob(pattern)):
            rel = p.relative_to(root)
            if not p.is_file() or p.name.endswith(".dbg.json") or "build-info" in rel.parts:
                continue
            try:
                data = json.loads(p.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            abi = data.get("abi") if isinstance(data, dict) else data
            if not isinstance(abi, list) or not abi or not all(isinstance(e, dict) and "type" in e for e in abi):
                continue
            name = data.get("contractName") if isinstance(data, dict) else None
            name = name if isinstance(name, str) and name else p.name.split(".")[0]
            entries.append(Artifact(str(rel), name, abi, _addresses(data)))
    return entries


ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _addresses(data) -> dict | None:
    """Where the file says it was deployed: Safe's `networkAddresses` (chain -> a deployment name, a list of
    them, or an address) with `deployments`; Truffle's `networks` (chain -> {"address"}); hardhat-deploy's
    top-level `address` (no chain: the folder names the network)."""
    if not isinstance(data, dict):
        return None
    found: dict = {}

    def add(chain, value):
        if isinstance(value, str) and ADDRESS.match(value):
            found.setdefault(chain, set()).add(value.lower())

    deployments = data.get("deployments") if isinstance(data.get("deployments"), dict) else {}
    for chain, value in (data.get("networkAddresses") or {}).items() if isinstance(data.get("networkAddresses"), dict) else []:
        for v in value if isinstance(value, list) else [value]:
            named = deployments.get(v) if isinstance(v, str) else None
            add(_chain(chain), named.get("address") if isinstance(named, dict) else v)
    for chain, value in (data.get("networks") or {}).items() if isinstance(data.get("networks"), dict) else []:
        add(_chain(chain), value.get("address") if isinstance(value, dict) else None)
    add(None, data.get("address"))
    return found or None


def _chain(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _abi_key(abi: list) -> tuple:
    return tuple(sorted(json.dumps(e, sort_keys=True) for e in abi))


def _checksum(address: str) -> str:
    from eth_utils import to_checksum_address
    return to_checksum_address(address)


@dataclass(frozen=True)
class Repo:
    url: str
    owner: str
    name: str
    commit: str
    root: Path  # the extracted source tree
    index: SolidityIndex
    artifacts: ArtifactIndex = ArtifactIndex([])

    def permalink(self, path: str, start: int, end: int) -> str:
        return f"https://github.com/{self.owner}/{self.name}/blob/{self.commit}/{path}#L{start}-L{end}"

    def file_url(self, path: str) -> str:
        return f"https://github.com/{self.owner}/{self.name}/blob/{self.commit}/{path}"

    @property
    def tree_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}/tree/{self.commit}"

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.name}@{self.commit[:7]}"


def _parts(url: str) -> tuple[str, str]:
    m = GITHUB.match(url)
    if not m:
        raise CollectorError(f"only GitHub repositories are supported, not {url}", retryable=False)
    return m.group(1), m.group(2)


class RepoCache:
    def __init__(self, cache_dir: str | Path):
        self.root = Path(cache_dir) / "repos"

    def _pins(self) -> dict[str, str]:
        path = self.root / "pins.json"
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except json.JSONDecodeError as exc:
            raise CollectorError(f"the repo cache's pins.json is damaged ({exc}); run anychain repos sync again",
                                 retryable=False) from exc

    def commit_for(self, cfg: RepoConfig) -> str | None:
        """The commit this repo is synced at, or None when it was never synced."""
        if SHA.match(cfg.ref) and self._dir(cfg, cfg.ref).exists():
            return cfg.ref
        commit = self._pins().get(f"{cfg.url}@{cfg.ref}")
        return commit if commit and self._dir(cfg, commit).exists() else None

    def _dir(self, cfg: RepoConfig, commit: str) -> Path:
        owner, name = _parts(cfg.url)
        return self.root / f"{owner}__{name}__{commit}"

    def load(self, cfg: RepoConfig) -> Repo | None:
        """The synced repo with its index, or None when it is not in the cache."""
        commit = self.commit_for(cfg)
        if commit is None:
            return None
        owner, name = _parts(cfg.url)
        root = self._dir(cfg, commit)
        return Repo(cfg.url, owner, name, commit, root, SolidityIndex.from_dir(root, cfg.source_globs),
                    ArtifactIndex.from_dir(root, cfg.artifact_globs))

    # ---- sync (network) ---------------------------------------------------------------------------

    def sync(self, cfg: RepoConfig, client: httpx.Client | None = None, resolve=None) -> str:
        """Download the repo at its commit into the cache; returns the commit."""
        owner, name = _parts(cfg.url)
        commit = cfg.ref if SHA.match(cfg.ref) else (resolve or _resolve_ref)(cfg.url, cfg.ref)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._dir(cfg, commit)
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as lock:  # one sync at a time per cache
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not target.exists():
                client = client or httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
                try:
                    response = client.get(f"https://codeload.github.com/{owner}/{name}/tar.gz/{commit}")
                except httpx.HTTPError as exc:
                    raise CollectorError(f"downloading {owner}/{name}@{commit[:7]} failed: {exc}", retryable=True)
                if response.status_code != 200:
                    raise CollectorError(f"downloading {owner}/{name}@{commit[:7]} failed: HTTP "
                                         f"{response.status_code}", retryable=response.status_code >= 500)
                _extract(response.content, target)
            pins = self._pins()
            pins[f"{cfg.url}@{cfg.ref}"] = commit
            tmp = self.root / "pins.json.tmp"
            tmp.write_text(json.dumps(pins, indent=1, sort_keys=True))
            os.replace(tmp, self.root / "pins.json")  # atomic: a reader never sees half a file
        return commit


def _resolve_ref(url: str, ref: str, run=subprocess.run) -> str:
    """A branch or tag name to the commit it points at, with git (no API token, no rate limit).

    `git ls-remote` matches the end of ref names (`main` also matches `refs/heads/archive/main`), so
    only the exact branch, or the exact tag peeled to its commit (an annotated tag is an object of its
    own, and a permalink with its id is a 404), is accepted.
    """
    try:
        done = run(["git", "ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"],
                   capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise CollectorError("git is not installed; set the repo's ref to a commit id instead", retryable=False)
    except subprocess.TimeoutExpired:
        raise CollectorError(f"git ls-remote {url} timed out", retryable=True)
    if done.returncode != 0:
        raise CollectorError(f"could not list the refs of {url}: {done.stderr.strip()[:200]}", retryable=True)
    refs = {name: sha for sha, name in (line.split("\t", 1) for line in done.stdout.splitlines() if "\t" in line)}
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if SHA.match(refs.get(name, "")):
            return refs[name]
    raise CollectorError(f"{url} has no branch or tag named {ref!r}", retryable=False)


def _extract(archive: bytes, target: Path) -> None:
    """Extract a GitHub tarball (one top folder) into `target`, refusing paths that leave it."""
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)  # left by an interrupted extraction: never reuse its files
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(tmp, filter="data")  # "data" filter: no absolute paths, links out, or devices
    tops = list(tmp.iterdir())
    (tops[0] if len(tops) == 1 and tops[0].is_dir() else tmp).rename(target)
    if tmp.exists():
        tmp.rmdir()
