"""The decoded call: ABIs and their cascade, signature candidates, EIP-7702 authorizations and delegations, calls to
native contracts and to code-less accounts. Part of BundleBuilder (bundle.py), split by concern (D79).
"""
import httpx
from eth_utils import keccak
from anychain.collectors.http import CollectorError
from anychain.collectors.types import Authorization, Transaction
from anychain.decoder import AbiDecoder, fit_signature
from anychain.models import Source
from anychain.solidity import filter_abi
from anychain.facts.common import ZERO_ADDRESS, _source_cause, address_of, party, with_args


class CallFacts:

    def _add_authorizations(self, tx_hash: str, tx: Transaction) -> None:
        """EIP-7702 (type 4): accounts that set or cleared the contract code they run.

        Only an authorization the explorer marks "ok" took effect, and when one account
        has several valid ones in the same tx, the last one wins (EIP-7702 order).
        """
        source = self._tx_source(tx_hash)
        if not tx.authorizations_readable:
            self._gap("Code delegations", "the explorer's EIP-7702 authorization list is not readable",
                      "Check the authorizations on the explorer page", retryable=False, cause="source_error")
        auths = tx.authorizations
        last_valid = {str(a.authority).lower(): n for n, a in enumerate(auths) if a.status == "ok"}
        for n, auth in enumerate(auths):
            text, applied, superseded = self._authorization_text(auth, last_valid.get(str(auth.authority).lower()) != n)
            self.bundle.add("delegation", text, [source],
                            {"authority": auth.authority, "delegate": auth.delegate, "status": auth.status,
                             "applied": applied, "superseded": superseded})

    def _authorization_text(self, auth: Authorization, not_last_valid: bool) -> tuple[str, bool | None, bool]:
        """(fact text, applied?, superseded?) for one authorization."""
        authority = auth.authority or "an account the explorer does not name"
        target, status = auth.delegate, auth.status
        clears = target == ZERO_ADDRESS
        what = ("clear its code delegation" if clears else
                f"run the code of {target}" if target else "change its code delegation (target not reported)")
        superseded = status == "ok" and not_last_valid
        applied = (not superseded) if status == "ok" else (False if status else None)
        if status == "ok" and target is None:
            text = f"Code delegation of {authority} changed by this transaction (EIP-7702); the explorer does " \
                   "not report the new target."
        elif superseded:
            text = f"EIP-7702 authorization for {authority} to {what} was valid but replaced by a later " \
                   "authorization for the same account in this transaction."
        elif status == "ok" and clears:
            text = f"Code delegation cleared by this transaction: {authority} stops running delegated code (EIP-7702)."
        elif status == "ok":
            text = f"Code delegation set by this transaction: from here on, {authority} runs the code of {target} (EIP-7702)."
        else:
            verdict = (f"was not applied: the explorer marks it {status!r}" if status
                       else "has no validity reported by the explorer")
            text = f"EIP-7702 authorization for {authority} to {what} {verdict}."
        return text, applied, superseded

    def _delegations_applied(self, tx: Transaction) -> dict[str, str]:
        """Lowercase authority -> delegate address in effect after this tx's valid authorizations
        (ZERO_ADDRESS when cleared). Authorizations are applied before execution; the last valid one wins."""
        result: dict[str, str] = {}
        for auth in tx.authorizations:
            if auth.status != "ok" or auth.authority is None:
                continue
            if auth.delegate is None:
                result.pop(auth.authority.lower(), None)  # target unknown: claim nothing
            else:
                result[auth.authority.lower()] = auth.delegate
        return result

    def _add_data_to_codeless(self, to_text: str, size: int, cleared_here: bool, today_basis: str,
                              source: Source) -> None:
        """Data sent to an account without code today. We only assert "no code" when this very
        transaction cleared the account's delegation; otherwise we say what is unknown.

        Why not ask the node for the code before the block: code can appear earlier in the
        same block, and precompiles (which differ per chain and fork) run with no stored code.
        """
        if cleared_here:
            self.bundle.add("call", f"Sent {size} bytes of data to {to_text}, whose code delegation this transaction "
                            "cleared before running; data sent to an account without code is not a function call.",
                            [source], {"data_bytes": size, "had_code": False})
            return
        self.bundle.add("call", f"Sent {size} bytes of data to {to_text}. {today_basis}; this tool cannot confirm "
                        "the account's code at the moment the transaction ran, so it does not say whether this "
                        "was a function call.", [source], {"data_bytes": size, "had_code": None})
        self._gap("Code at execution time", "an account's code during a transaction cannot be confirmed from the "
                  "explorer or a plain state read (code can change within a block, and built-in precompiles run "
                  "without stored code)", "An execution trace of the transaction (debug/trace RPC)", retryable=False, cause="not_interpretable")

    def _add_call(self, tx_hash: str, tx: Transaction) -> None:
        b, sym = self.bundle, self.cfg.network.native_symbol
        source = self._tx_source(tx_hash)
        data, to = tx.raw_input, tx.to
        if data is None:
            self._gap("Call decoding", "the explorer's call data is not readable", "Check the input on the explorer page",
                      retryable=False, cause="source_error")
            return
        if to is not None and to.address is None:
            self._gap("Call decoding", "the explorer did not report the target address of the call",
                      "Check the transaction on the explorer page", retryable=False, cause="source_error")
            return
        if to is None:
            created = tx.created_contract
            text = f"Contract creation: deployed {self._party(created)}." if created else "Contract creation transaction."
            b.add("call", text, [source], {"created": address_of(created)})
            return
        if address_of(tx.sender) == ZERO_ADDRESS:
            text = f"System transaction: sent from the zero address, which no one can sign for, to {self._party(to)}."
            if data == "0x":
                text += " It carries no call data."
            b.add("call", text, [source], {"system": True})
            if data == "0x":
                return
        if data == "0x":
            if tx.authorizations and not tx.value:
                b.add("call", f"No call data and no value were sent; the transaction carries {len(tx.authorizations)} "
                      "EIP-7702 authorization(s), listed as delegation facts.", [source])
            else:
                b.add("call", f"Plain {sym} transfer (no call data) to {self._party(to)}.", [source])
            return
        if self._is_native_contract(to.address):
            self._add_native_contract_call(self._party(to), to.address, data, source)
            return
        delegate_here = self._delegations_applied(tx).get(to.address.lower())
        if delegate_here == ZERO_ADDRESS or (
                delegate_here is None and to.is_contract is False and not to.implementations):
            self._add_data_to_codeless(self._party(to), (len(data) - 2) // 2, delegate_here == ZERO_ADDRESS,
                                       "The explorer lists it as having no contract code today", source)
            return

        # Code the account ran: a delegation set by this very tx first, then today's listed implementations.
        decoder = self._decoder_for(to.address, ([delegate_here] if delegate_here else []) + list(to.implementations))
        decoded = decoder.decode_call(data) if decoder else None
        if decoded is None:
            b.add("call", f"Called function with selector {data[:10]} on {self._party(to)}; not decoded.",
                  [source], {"selector": data[:10]})
            self._declare_undecoded("Call decoding", to.address, f"selector {data[:10]}", code_owner=delegate_here)
            self._safely("Signature database", lambda: self._add_signature_candidates("call", data[:10], data))
            return
        abi_source, confidence = self._abi_provenance(to.address)
        b.add("call", self._call_text(to.address, decoded, self._party(to)), [source, abi_source],
              {"function": decoded.name, "args": {a.name: a.value for a in decoded.args},
               "abi_origin": self.abi_origin.get(to.address.lower(), ("explorer",))[0]}, confidence=confidence)

    def _abi_provenance(self, address: str) -> tuple[Source, str | None]:
        """The ABI's source for a decoded fact, and the confidence it allows (None: follow the sources)."""
        origin = self.abi_origin.get(address.lower())
        if origin is None:
            return self._api_source(f"/smart-contracts/{address}", "Explorer API: contract ABI"), None
        kind, repo, contract = origin
        self.abi_notes[address.lower()] = self.repo_notes[address.lower()]  # used for this fact: now it is the source
        self.bundle.abi_sources[address] = self.repo_notes[address.lower()]
        if kind == "repo_pinned":
            c = repo.index.contracts[contract]
            return Source(kind="repo", label=f"Repository {repo.label}", url=repo.permalink(c.path, c.start, c.end)), None
        if kind == "repo_artifact":  # `contract` holds the artifact's path
            return Source(kind="repo", label=f"Repository {repo.label}", url=repo.file_url(contract)), None
        return Source(kind="repo", label=f"Repository {repo.label if repo else 'source signatures'}",
                      url=repo.tree_url if repo else None), "candidate"

    def _call_text(self, address: str, decoded, party: str) -> str:
        if self.abi_origin.get(address.lower(), ("",))[0] == "repo_match":
            return (f"The call's selector matches {decoded.signature} in a configured repository; decoded with it, the "
                    f"call on {party} would be{with_args(decoded.args) or ' without arguments'}. Not confirmed: "
                    f"{self.abi_notes[address.lower()]}.")
        return f"Called {decoded.signature} on {party}{with_args(decoded.args)}. ABI source: {self.abi_notes[address.lower()]}."

    def _add_native_contract_call(self, to_text: str, address: str, data: str, source: Source) -> None:
        """A call to a contract built into the node: it runs without bytecode, but it can have a
        published ABI (e.g. Rootstock's Bridge), so the normal ABI lookup still applies."""
        decoder = self._decoder_for(address)
        decoded = decoder.decode_call(data) if decoder else None
        if decoded:
            abi_source, confidence = self._abi_provenance(address)
            self.bundle.add("call", self._call_text(address, decoded, to_text), [source, abi_source],
                            {"function": decoded.name, "args": {a.name: a.value for a in decoded.args},
                             "native_contract": True,
                             "abi_origin": self.abi_origin.get(address.lower(), ("explorer",))[0]},
                            confidence=confidence)
            return
        self.bundle.add("call", f"Called {to_text}, a contract built into the network's node, with selector "
                        f"{data[:10]}; not decoded.", [source], {"selector": data[:10], "native_contract": True})
        self._declare_undecoded("Call decoding", address, f"selector {data[:10]}")

    def _decoder_for(self, address: str, implementations: list[str] | None = None) -> AbiDecoder | None:
        """ABI decoder for a contract, cached per run. Records where the ABI came from."""
        key = address.lower()
        if key in self.decoders:
            return self.decoders[key]
        explorer_on = "explorer" in self.cfg.abi_strategy.order
        decoder, note = None, ("none (explorer unavailable)" if explorer_on else "none (explorer ABI disabled in config)")
        if explorer_on and self.explorer_answered:
            lookup = self.explorer.abi_for(address, implementations)
            decoder = AbiDecoder(lookup.abi) if lookup.abi else None
            note = f"explorer: {lookup.note}" if decoder else "none"
            if lookup.failures:
                retryable = any(f.retryable for f in lookup.failures)
                self.abi_lookup_failed[key] = retryable
                self._gap("ABI lookup", f"the ABI lookup for {address} failed: {lookup.failures[0]}",
                          "Try again in a few minutes" if retryable else "Check the contract on the explorer page",
                          retryable, _source_cause(retryable))
        if decoder is None:
            repo_decoder = self._repo_decoder(address)
            if repo_decoder:  # its note is recorded only when it decodes something (_abi_provenance)
                decoder, self.repo_notes[key] = repo_decoder
        self.decoders[key] = decoder
        self.abi_notes[key] = note
        self.bundle.abi_sources[address] = note
        return decoder

    def _add_signature_candidates(self, what: str, key: str, data: str | None) -> None:
        """Public signature database candidates for a selector or event topic no ABI decodes (PHASE2 T7, D35).
        Always a candidate: anyone can add entries and different signatures share selectors. A function or
        error signature is offered only when the data fits its types exactly."""
        db = self.signatures
        if db is None or not db.enabled or db.failed:  # after one failure: no more lookups, no more gaps
            return
        try:
            names = db.events(key) if what == "event" else db.functions(key)
        except CollectorError as exc:
            self._gap("Signature database", f"the lookup of {key} failed: {exc}",
                      "Try again in a few minutes" if exc.retryable else "Check the signature database URL in the config",
                      retryable=exc.retryable, cause=_source_cause(exc.retryable))
            return
        if not names:
            return
        host = httpx.URL(db.cfg.url).host
        source = Source(kind="signature_db", label=f"Signature database {host}",
                        url=f"{db.cfg.url.rstrip('/')}/{'event-signatures' if what == 'event' else 'signatures'}/"
                            f"?hex_signature={key}")
        caveat = ("Anyone can add entries there and different signatures can share a selector, so this is not "
                  "confirmed.")
        thing = {"call": "The call's selector", "error": "The replay's custom error selector",
                 "event": "The event's topic"}[what]
        if what == "event":
            # An event topic is the 32-byte hash of its signature: a text whose hash equals the topic is proven to
            # be that signature (finding another text with the same hash is not feasible). Others are discarded.
            proven = [n for n in names if "0x" + keccak(text=n).hex() == key.lower()]
            if proven:
                self.bundle.add("event_signature",
                                f"The event's topic {key[:10]}… is the hash of {proven[0]}: that text comes from the "
                                f"public signature database {host}, and its hash was checked to equal the topic. Its "
                                "arguments are not decoded: which of them are indexed is not known.",
                                [source], {"topic": key, "signature": proven[0]}, confidence="single_source")
            return
        fitting = [(n, args) for n in names if (args := fit_signature(n, data or "")) is not None]
        if not fitting:
            text = (f"{thing} {key} has entries in the public signature database {host} ({self._and_list(names)}), "
                    "but the data does not fit their types exactly, so none of them is offered.")
            self.bundle.add("candidate", text, [source], {"selector": key, "signatures": names, "fitting": []},
                            confidence="candidate")
            return
        if len(fitting) == 1:
            name, args = fitting[0]
            shown = ", ".join(f"{a.name}={a.value}" for a in args) or "no arguments"
            text = (f"{thing} {key} matches {name} in the public signature database {host}, and the data fits its "
                    f"types exactly; decoded with it: {shown}. {caveat}")
        else:
            text = (f"{thing} {key} matches several signatures in the public signature database {host} that all fit "
                    f"the data: {self._and_list([n for n, _ in fitting])}. Which one, if any, is not known. {caveat}")
        self.bundle.add("candidate", text, [source],
                        {"selector": key, "signatures": names, "fitting": [n for n, _ in fitting]}, confidence="candidate")

    @staticmethod
    def _and_list(items: list[str]) -> str:
        return items[0] if len(items) == 1 else "; ".join(items[:-1]) + "; or " + items[-1]

    def _repo_decoder(self, address: str) -> tuple[AbiDecoder, str] | None:
        """No ABI from the explorer: decode with the configured repos' source (PHASE2 T6, D34).
        A contract pinned in address_map uses its own ABI from source (single source: the config says which
        contract it is); otherwise every selector the repos declare once is tried, and a match is a candidate."""
        order = self.cfg.abi_strategy.order
        repo_sources = [s for s in order if s in ("repo_artifacts", "repo_source_signatures")]
        if not self.cfg.repos or not repo_sources:
            return None
        repos = self._loaded_repos()
        pin = self.cfg.address_map.get(address.lower())
        if pin is not None:
            return self._pinned_decoder(address, pin, repos, repo_sources)
        if "repo_source_signatures" not in order:
            return None
        abi = filter_abi([e for r in repos for e in r.index.abi()])  # ambiguity across repos too
        if not abi:
            return None
        self.abi_origin[address.lower()] = ("repo_match", repos[0] if len(repos) == 1 else None, None)
        labels = ", ".join(r.label for r in repos)
        return AbiDecoder(abi), (f"repository {labels} source signatures (a selector match in the repository; "
                                 "which contract this is was not confirmed)")

    def _pinned_decoder(self, address: str, pin, repos: list, repo_sources: list[str]) -> tuple[AbiDecoder, str] | None:
        """The ABI of the contract address_map pins to `address`: from the repo's compiled artifacts or its source,
        in the configured order (PHASE2_5 T3, D40). Single source: the config says which contract it is."""
        repo = next((r for r in repos if r.url == pin.repo), None)
        if repo is None:
            problems = ["the repository is not synced"]
        else:
            problems = []
            for kind in repo_sources:
                if kind == "repo_artifacts":
                    found = repo.artifacts.match(pin.contract, address, self.cfg.network.chain_id)
                    if isinstance(found, str):
                        problems.append(found)
                        continue
                    self.abi_origin[address.lower()] = ("repo_artifact", repo, found.path)
                    why = (f"which lists this address for chain {self.cfg.network.chain_id}" if found.listed
                           else "pinned to this address in the config")
                    return AbiDecoder(found.abi), f"repository {repo.label}: compiled ABI {found.path} ({pin.contract}, {why})"
                else:
                    problem = (f"{pin.contract} is declared in more than one of its source files"
                               if pin.contract in repo.index.ambiguous else
                               f"its source has no contract named {pin.contract}"
                               if pin.contract not in repo.index.contracts else None)
                    abi = repo.index.abi(pin.contract) if problem is None else []
                    if problem is None and not abi:
                        problem = f"no ABI could be built from {pin.contract}'s source (unresolved types or inheritance)"
                    if problem is None:
                        self.abi_origin[address.lower()] = ("repo_pinned", repo, pin.contract)
                        return AbiDecoder(abi), f"repository {repo.label}: {pin.contract} (pinned to this address in the config)"
                    problems.append(problem)
        self._gap("ABI lookup", f"address_map pins {address} to {pin.contract} in {pin.repo}, but " + "; ".join(problems),
                  "Sync the repo, or fix the contract name in address_map or the repo's artifact_globs", retryable=False,
                  cause="config_error")
        return None

    def _declare_undecoded(self, topic: str, address: str, what: str, code_owner: str | None = None) -> None:
        """Gap for something we could not decode, saying whether the ABI is missing or just unreachable."""
        if address.lower() in self.abi_lookup_failed:
            retryable = self.abi_lookup_failed[address.lower()]
            self._gap(topic, f"{what} on {address} not decoded because the ABI lookup failed",
                      "Try again in a few minutes" if retryable else "Check the contract on the explorer page",
                      retryable, _source_cause(retryable))
        elif self._is_native_contract(address):
            self._gap(topic, f"{address} is a native contract built into the node and the explorer has no verified "
                      f"ABI for it, so {what} cannot be decoded", "Its published ABI (e.g. from the network node's "
                      "source) in a configured repo", retryable=False, cause="not_interpretable")
        elif code_owner:
            self._gap(topic, f"{address} ran the code of {code_owner} (EIP-7702), and no ABI for it matches {what}",
                      f"{code_owner} verified on the explorer, or its ABI in a configured repo", retryable=False,
                      cause="not_interpretable")
        else:
            self._gap(topic, f"no ABI for {address} matches {what}",
                      "A verified contract on the explorer, or the contract ABI in a configured repo",
                      retryable=False, cause="not_interpretable")
