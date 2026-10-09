# One transaction through the code, in two minutes

Follow `anychain explain 0x73c4…c8a --config configs/ethereum-mainnet.yaml` (a Uniswap swap that failed because its deadline had passed) from the command to the answer. Each step names the function to open.

1. **The command** (`src/anychain/cli.py:40`, `explain`) loads the YAML config and calls the one path shared by the CLI and the API.
2. **One answer, end to end** (`src/anychain/service.py:44`, `answer_transaction`): reuse a cached bundle for a final transaction or build it; ask one clarifying question if the triage needs one; have the model write; log one row per answer. About 70 lines: read it first.
3. **The facts, no AI** (`src/anychain/bundle.py:209`, `BundleBuilder.build`): the explorer (Blockscout API v2) and the node (JSON-RPC) are read in parallel; every fact is added with `bundle.add(kind, text, sources)` and gets an id (E1, E2…) and a confidence; every failure to read becomes a gap with a cause, never an exception on screen. For this transaction: the overview, the fee, the decoded call `swapExactETHForTokens`, the function's verified code with the reason's line, and Uniswap's repository permalink to the same lines (`_cite_function`, line 516).
4. **The conclusion, no AI** (`_add_diagnosis`, `bundle.py:678`, calling `diagnose` in `src/anychain/diagnosis.py:77`): the rules start from where the failure began, read the reason ("UniswapV2Router: EXPIRED"), compare the call's `deadline` parameter with the block's time, and label the finding CONFIRMED, LIKELY or UNKNOWN. When there is no reason, the call is replayed on the node (`bundle.py:884`).
5. **Context facts, appended last so no fact is renumbered**: the sender's timeline (`timeline.build`, `bundle.py:761`): the same swap succeeded 48 seconds later; gas notes (`gas.usage_note`, `bundle.py:722`); security notes on the called code (`security.scan`, `bundle.py:621`).
6. **The prose, with AI** (`src/anychain/writer.py:308`, `write_checked`): the model receives only the facts and the gaps (`evidence_payload`, `writer.py:61`) and an outline of the answer built from them (`outline.build`, `src/anychain/outline.py:15`), and must cite a fact id in every sentence.
7. **The check, no AI** (`src/anychain/validator.py:53`, `check_answer`): every number, address, hash, link and citation in the answer must be in the facts (a number may be rounded, never changed). A problem sends the answer back once with the problems listed; a second failure withholds it and the facts are shown instead.
8. **The output**: the CLI renders markdown (`render.py`); the API returns the structured answer (`answer.py:14`, `structured_answer`), which the page shows as a conversation.

To see the facts this produces without any model: add `--no-llm --evidence`.
