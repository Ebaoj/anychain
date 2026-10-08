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


@dataclass(frozen=True)
class Repo:
    url: str
    owner: str
    name: str
    commit: str
    root: Path  # the extracted source tree
    index: SolidityIndex

    def permalink(self, path: str, start: int, end: int) -> str:
        return f"https://github.com/{self.owner}/{self.name}/blob/{self.commit}/{path}#L{start}-L{end}"

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
        return Repo(cfg.url, owner, name, commit, root, SolidityIndex.from_dir(root, cfg.source_globs))

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
