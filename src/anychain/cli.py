"""Command line interface: `anychain explain <hash>`."""
import sys
from enum import Enum

import typer
from dotenv import load_dotenv

from anychain.bundle import InvalidHashError, build_bundle
from anychain.config import ConfigError, load_config
from anychain.render import render_markdown
from anychain.writer import WriterError, write_explanation

app = typer.Typer(help="Explain and troubleshoot EVM transactions on any configured network.", no_args_is_help=True)


class Mode(str, Enum):
    support = "support"
    developer = "developer"
    auditor = "auditor"


@app.callback()
def _main() -> None:
    load_dotenv()


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def explain(
    tx_hash: str = typer.Argument(..., help="Transaction hash (0x + 64 hex)"),
    mode: Mode = typer.Option(None, help="Who the answer is for (default: from config)"),
    config: str = typer.Option(None, "--config", help="Path to network YAML (or set ANYCHAIN_CONFIG)"),
    as_json: bool = typer.Option(False, "--json", help="Print the evidence bundle as JSON"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the LLM; print the evidence only"),
) -> None:
    """Explain what a transaction did, with a cited source for each fact."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        _fail(str(exc))
    try:
        bundle = build_bundle(tx_hash.strip(), cfg)
    except InvalidHashError as exc:
        _fail(str(exc))
    except Exception as exc:  # last line of defence: never a stack trace for the user
        _fail(f"Unexpected error while collecting data ({type(exc).__name__}: {exc}). Please report it.")

    if as_json:
        print(bundle.model_dump_json(indent=2))
        return
    evidence_md = render_markdown(bundle)
    if no_llm or not bundle.items:
        print(evidence_md)
        return
    try:
        answer = write_explanation(bundle, cfg, (mode or Mode(cfg.assistant.default_mode)).value)
        print(answer + "\n\n---\n" + evidence_md)
    except WriterError as exc:
        print(f"_(LLM unavailable: {exc}. Showing the evidence only.)_\n", file=sys.stderr)
        print(evidence_md)


if __name__ == "__main__":
    app()
