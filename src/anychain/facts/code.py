"""The contracts' code: configured repositories, verified source, the called function's code and its repository
permalink, the reason's line, heuristic security notes. Part of BundleBuilder (bundle.py), split by concern (D79).
"""
from anychain import security
from anychain.collectors.http import CollectorError
from anychain.collectors.repo import Repo, RepoCache
from anychain.collectors.types import AddressRef, Transaction
from anychain.diagnosis import reason_text
from anychain.models import Source
from anychain.solidity import SolidityIndex
from anychain.facts.common import MAX_PLACES, ZERO_ADDRESS, _raises_note, render_code


class CodeFacts:

    # ---- source code from configured repos and the explorer's verified source (PHASE2 T6, D33) ----

    def _loaded_repos(self) -> list[Repo]:
        if self.repos is None:
            self.repos, cache = [], RepoCache(self.cfg.storage.cache_dir)
            for repo_cfg in self.cfg.repos:
                try:
                    repo = cache.load(repo_cfg)
                except CollectorError as exc:
                    self._gap("Source code", str(exc), "A GitHub repository URL in the config", retryable=False,
                              cause="config_error")
                    continue
                except Exception as exc:  # e.g. a damaged cache: this repo only, the others still load
                    self._gap("Source code", f"the repository {repo_cfg.url} could not be read from the cache "
                              f"({type(exc).__name__}: {exc})", "Run anychain repos sync again", retryable=False,
                              cause="processing_error")
                    continue
                if repo is None:
                    self._gap("Source code", f"the configured repository {repo_cfg.url} is not in the local cache",
                              "Run once: anychain repos sync --config <this network's config>", retryable=False,
                              cause="config_error")
                else:
                    self.repos.append(repo)
        return self.repos

    def _verified(self, address: str) -> tuple[str | None, SolidityIndex | None]:
        """(contract name, index of its verified source files) from the explorer's metadata, built once."""
        key = address.lower()
        if key not in self.verified_cache:
            self.verified_cache[key] = self._read_verified(address)
        return self.verified_cache[key]

    def _contract_name(self, address: str) -> str | None:
        """The name the explorer gives a contract's verified code (no source indexing)."""
        name = self.explorer.smart_contract(address).get("name")
        return name if isinstance(name, str) else None

    def _read_verified(self, address: str) -> tuple[str | None, SolidityIndex | None]:
        meta = self.explorer.smart_contract(address)  # fetched once per explanation (memoized)
        files = {}
        if isinstance(meta.get("source_code"), str) and meta.get("file_path"):
            files[str(meta["file_path"])] = meta["source_code"]
        for extra in meta.get("additional_sources") or []:
            if isinstance(extra, dict) and isinstance(extra.get("source_code"), str) and extra.get("file_path"):
                files[str(extra["file_path"])] = extra["source_code"]
        name = meta.get("name") if isinstance(meta.get("name"), str) else None
        return name, (SolidityIndex(files) if files else None)

    def _contracts_behind(self, to: AddressRef) -> list[str]:
        """The call target's code addresses: its implementations first (what runs), then itself. Empty for an
        account the explorer does not list as a contract (no metadata request for it)."""
        if not (to.is_contract or to.implementations):
            return []
        return [a for a in [*to.implementations, to.address] if isinstance(a, str) and a]

    def _add_source_code(self, tx: Transaction) -> None:
        if (not self.cfg.repos or "explorer" not in self.cfg.abi_strategy.order or not self.explorer_answered
                or tx.to is None or not tx.to.address):
            return
        repos = self._loaded_repos()
        data = tx.raw_input or ""
        if not repos or len(data) < 10:
            return
        selector = data[:10].lower()
        for address in self._contracts_behind(tx.to):
            try:
                name = self._contract_name(address)
            except CollectorError:
                continue
            if not name or not any(name in r.index.contracts or name in r.index.ambiguous for r in repos):
                continue
            _name, verified = self._verified(address)  # its source is indexed only now that a repo has the name
            for repo in repos:
                if name and name in repo.index.ambiguous:
                    self._gap("Source code", f"{name} is declared in more than one file of {repo.label}, so which one "
                              "is the deployed contract cannot be told", "A config naming the file (not supported yet)",
                              retryable=False, cause="not_interpretable")
                    return
                if name and name in repo.index.contracts:
                    self.source_repo = repo
                    self._cite_function(repo, name, selector, address, verified)
                    return

    def _cite_function(self, repo: Repo, name: str, selector: str, address: str,
                       verified: SolidityIndex | None) -> None:
        found = repo.index.find(name, selector)
        in_verified = verified.find(name, selector) if verified else None
        api = self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")
        if verified is None:
            compare = ("The explorer has no verified source for this contract, so whether the deployed code is this "
                       "version is not known: the match is by the contract's name.")
        elif in_verified is None:
            compare = ("The explorer's verified source does not define a function with this selector under this "
                       "contract's name, so the two copies could not be compared.")
        if found is None:
            contract = repo.index.contracts[name]
            outside = repo.index.external_bases(name)
            text = (f"{name} is in the configured repository {repo.label}, but no implemented external or public "
                    f"function with selector {selector} was found in its files for {name} or its bases there")
            text += f" (its bases outside the repository: {', '.join(outside)})." if outside else "."
            sources = [Source(kind="repo", label=f"Repository {repo.label}",
                              url=repo.permalink(contract.path, contract.start, contract.end))]
            if in_verified:
                f, c = in_verified.function, in_verified.contract
                text += (f" The deployed code's verified source on the explorer defines {f.signature} in {c.name} "
                         f"({c.path}, lines {f.start}-{f.end})")
                text += (", a contract this repository does not have, so its code is not the deployed code."
                         if c.name not in repo.index.contracts else ".")
                sources.append(api)
            self.bundle.add("source", text, sources, {"repo": repo.label, "contract": name, "selector": selector,
                                                      "in_repo": False}, confidence="single_source")
            return
        f, c = found.function, found.contract
        text = (f"In the configured repository {repo.label}, {f.signature} of {name} is defined in {c.name} "
                f"({c.path}, lines {f.start}-{f.end}).")
        sources = [Source(kind="repo", label=f"Repository {repo.label}", url=repo.permalink(c.path, f.start, f.end))]
        same = None
        if in_verified:
            same = in_verified.function.text == f.text
            vc = in_verified.contract
            text += (" Its text is the same in the contract's verified source on the explorer (comments and spacing "
                     "aside)." if same else
                     f" Its text differs from the contract's verified source on the explorer ({vc.path}, lines "
                     f"{in_verified.function.start}-{in_verified.function.end}): the repository's code is not the "
                     "deployed code (a different version, or a different contract with the same name).")
            sources.append(api)
            confidence = "single_source"
        else:
            text += " " + compare
            confidence = "candidate"
        self.bundle.add("source", text, sources,
                        {"repo": repo.label, "contract": name, "defined_in": c.name, "path": c.path,
                         "lines": [f.start, f.end], "selector": selector, "in_repo": True, "same_as_verified": same},
                        confidence=confidence)

    def _code_addresses(self, tx: Transaction) -> list[tuple[str, str]]:
        """(address whose verified source to read, what it is) for the code the call ran: a delegation this
        transaction set comes first; a cleared one means no code; otherwise the explorer's current
        implementations, then the address itself (review of D38)."""
        to = tx.to
        if to is None or not to.address:
            return []
        delegate = self._delegations_applied(tx).get(to.address.lower())
        last = [a for a in tx.authorizations if a.status == "ok" and (a.authority or "").lower() == to.address.lower()]
        if delegate == ZERO_ADDRESS or (last and last[-1].delegate is None):
            return []  # cleared, or changed by this transaction to code we cannot name
        if delegate:
            return [(delegate, f"the delegate {delegate} this transaction set for {to.address}")]
        return [(a, f"the implementation the explorer lists today for {to.address} (it may have been upgraded "
                    f"since this transaction)" if a.lower() != to.address.lower() else f"the contract {a}")
                for a in self._contracts_behind(to)]

    def _code_fact(self, index: SolidityIndex, found, address: str, origin: str, raises: str | None = None,
                   line: int | None = None, show: tuple[int, ...] = ()):
        """The code of one function as a fact: numbered lines, cut at MAX_CODE_LINES around the raising line
        (D38)."""
        f, c = found.function, found.contract
        lines = index.lines(c.path, f.start, f.end)
        what = f.signature or f"{f.name}(…)"
        head = (f"Code of {what} in {c.name} ({c.path}, lines {f.start}-{f.end}), from the verified source of "
                f"{origin}, as the explorer lists it:\n")
        for e in self.bundle.items:  # the same function already shown: add the raising line to it
            if e.kind == "code" and e.data.get("path") == c.path and e.data.get("lines") == [f.start, f.end]:
                if raises and not e.data.get("raises"):
                    e.data["raises"], e.data["raise_line"] = raises, line
                    e.text = (head + render_code(lines, f.start, line, tuple(e.data.get("show") or ()))
                              + _raises_note(line, raises))
                return e
        text = head + render_code(lines, f.start, line, show) + (_raises_note(line, raises) if raises else "")
        api = self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")
        return self.bundle.add("code", text, [api], {"function": what, "contract": c.name, "path": c.path,
                                                      "lines": [f.start, f.end], "raises": raises, "raise_line": line,
                                                      "show": list(show)})

    def _add_function_code(self, tx: Transaction) -> None:
        """The called function's code, from the verified source of the code it ran (PHASE2_5 T1, R1)."""
        if (not self.explorer_answered or "explorer" not in self.cfg.abi_strategy.order or tx.to is None
                or len(tx.raw_input or "") < 10):
            return
        selector = tx.raw_input[:10].lower()
        for address, origin in self._code_addresses(tx):
            try:
                name = self._contract_name(address)
            except CollectorError:
                continue
            _name, verified = self._verified(address) if name else (None, None)
            found = verified.find(name, selector) if verified else None
            if found:
                notes = security.scan(verified, found, name)
                self.called_code = (verified, found, name, address)  # for the gas notes (PHASE4 T3)
                code = self._code_fact(verified, found, address, origin, show=tuple(n.line for n in notes))
                self._add_security_notes(notes, found, address, code.id)
                return

    def _add_security_notes(self, notes: list, found, address: str, code_id: str) -> None:
        """Known risky patterns in the called function's code, each with its line (PHASE2_5 T2, R2, D39). An
        absence states nothing: no fact is written when no pattern matches."""
        if not notes:
            return
        what = found.function.signature or found.function.name
        text = (f"Security notes for {what} (code in {code_id}): heuristic pattern matches on the source, not an "
                f"audit and not a finding that the contract is vulnerable; the helpers it calls were not read. "
                + " ".join(f"({i}) {n.text}." for i, n in enumerate(notes, 1)))
        api = self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")
        self.bundle.add("security_note", text, [api], {"notes": [n.__dict__ for n in notes], "code": code_id})

    def _add_reason_in_source(self, tx: Transaction) -> None:
        """Where the failure's reason text is written: in the verified source of the contract called, and in
        the configured repo matched to that contract (not in unrelated repos)."""
        if self.bundle.status != "failed":
            return
        text = reason_text(tx.revert_reason)
        if not text:
            return
        places: list[tuple[str, Source]] = []
        found_total = 0
        if self.source_repo is not None:
            hits = self.source_repo.index.literal(text)
            found_total += len(hits)
            for path, line in hits[:MAX_PLACES]:
                places.append((f"{self.source_repo.label} {path}, line {line}",
                               Source(kind="repo", label=f"Repository {self.source_repo.label}",
                                      url=self.source_repo.permalink(path, line, line))))
        if tx.to is not None and self.explorer_answered and "explorer" in self.cfg.abi_strategy.order:
            for address, origin in self._code_addresses(tx)[:1]:
                try:
                    name, verified = self._verified(address)
                except CollectorError:
                    name, verified = None, None
                hits = verified.literal(text) if verified else []
                found_total += len(hits)
                for path, line in hits[:MAX_PLACES]:
                    places.append((f"the verified source {path}, line {line}",
                                   self._api_source(f"/smart-contracts/{address}", "Explorer API: verified source")))
                selector = (tx.raw_input or "")[:10].lower() if len(tx.raw_input or "") >= 10 else None
                raising = verified.raising_function(name, text, selector) if verified and name else None
                if raising:
                    self._code_fact(verified, raising[0], address, origin, raises=text, line=raising[1])
        if places:
            more = found_total - len(places)
            sources = list({(s.kind, s.url): s for _, s in places}.values())  # one source per page
            self.bundle.add("source", f"The reason {text!r} is written in " + "; ".join(p for p, _ in places)
                            + (f" (and {more} more place{'s' if more > 1 else ''})." if more > 0 else "."),
                            sources, {"reason": text, "places": [p for p, _ in places]}, confidence="single_source")
