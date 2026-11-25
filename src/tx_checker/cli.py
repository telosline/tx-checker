import json
import logging
import os
import textwrap
from enum import StrEnum
from typing import Annotated, Any

import typer

from tx_checker.check import check
from tx_checker.config import MissingApiKeyError
from tx_checker.deposit_check import (
    DepositInputError,
    DepositResult,
    Verdict,
    find_deposit,
    parse_expected,
    supported_assets,
)
from tx_checker.explain.explain import explain, explain_deposit
from tx_checker.models import Network, Status, TxReport
from tx_checker.redact import redact

EXIT_FOUND = 0
EXIT_NOT_FOUND = 1
EXIT_INVALID_INPUT = 2
EXIT_PROVIDER_ERROR = 3
EXIT_NO_API_KEY = 4
EXIT_MISMATCH = 5
EXIT_WAITING = 6
EXIT_UNEXPECTED = 70

HELP_EPILOG = """\b
Exit codes:
  0  the transaction was found (success, failed or pending)
  1  not found
  2  invalid input or options
  3  provider error (limit reached, service unavailable, chain not supported)
  4  API key is not configured (see .env.example)
  70 unexpected error

\b
Copy the client message to the clipboard:
  Linux:  tx-checker check <hash> --client-only | wl-copy
  macOS:  tx-checker check <hash> --client-only | pbcopy
"""

_ASSETS = textwrap.fill(
    ", ".join(supported_assets()), width=68, initial_indent="  ", subsequent_indent="  "
)

DEPOSIT_EPILOG = f"""\b
Exit codes:
  0  match: the transaction matches and is final
  5  mismatch: see the reasons in the output
  6  waiting: it matches but is not final yet, or it is not in a block yet
  1  not found
  2  invalid input or options
  3  provider error (limit reached, service unavailable, chain not supported)
  4  API key is not configured (see .env.example)
  70 unexpected error

\b
Assets (USDT-TRC20 and USDT-ERC20, and the same for USDC, also work):
{_ASSETS}

\b
Example:
  tx-checker deposit <hash> --address <address> --amount 100 --asset USDT-TRON \\
    --client-only | wl-copy
"""

ROOT_EPILOG = (
    HELP_EPILOG + "\n\b\nThe deposit command adds the exit codes 5 and 6, see "
    "tx-checker deposit --help.\n"
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    epilog=ROOT_EPILOG,
    help="Check a crypto transaction by hash and explain the result in plain language.",
)
log = logging.getLogger(__name__)


class Lang(StrEnum):
    EN = "en"
    UK = "uk"
    RU = "ru"


_VERDICT_COLORS = {
    Verdict.MATCH: "green",
    Verdict.WAITING: "yellow",
    Verdict.MISMATCH: "red",
    Verdict.INCONCLUSIVE: "yellow",
}

_STATUS_COLORS = {
    Status.SUCCESS: "green",
    Status.FAILED: "red",
    Status.PENDING: "yellow",
    Status.NOT_FOUND: "yellow",
    Status.INVALID_INPUT: "red",
    Status.PROVIDER_ERROR: "red",
}


@app.callback()
def main() -> None:
    """Check a crypto transaction by hash and explain the result in plain language."""
    # Provider problems are shown as reports, so only real bugs reach the log output.
    logging.basicConfig(level=logging.ERROR, format="%(message)s")


@app.command("check", epilog=HELP_EPILOG)
def check_command(
    hash_or_url: Annotated[
        str, typer.Argument(metavar="HASH_OR_URL", help="Transaction hash or explorer link.")
    ],
    network: Annotated[
        Network | None,
        typer.Option(
            help="Force the network. Without it a bare 64-hex hash is tried on TRON first."
        ),
    ] = None,
    chain_id: Annotated[
        int | None, typer.Option(min=1, help="EVM chain id. Default: 1 (Ethereum).")
    ] = None,
    lang: Annotated[Lang, typer.Option(help="Language of the client message.")] = Lang.EN,
    confirmations: Annotated[
        int | None,
        typer.Option(min=1, help="Confirmations required to call a transaction final."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the report and the texts as JSON and nothing else."),
    ] = False,
    client_only: Annotated[
        bool, typer.Option("--client-only", help="Print only the message for the client.")
    ] = False,
    summary_only: Annotated[
        bool, typer.Option("--summary-only", help="Print only the internal summary.")
    ] = False,
) -> None:
    """Look up a transaction and explain it.

    Prints an internal summary and a message for the client. The client message is plain
    text, so it can be pasted into a ticket as it is. The headings go to stderr and the
    texts to stdout, so a pipe gets only the text.
    """
    if sum([as_json, client_only, summary_only]) > 1:
        raise typer.BadParameter("Use only one of --json, --client-only and --summary-only.")

    try:
        report = check(
            hash_or_url,
            network=network,
            chain_id=chain_id,
            required_confirmations=confirmations,
        )
        explanation = explain(report, lang.value)
    except MissingApiKeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(EXIT_NO_API_KEY) from None
    except Exception as exc:
        # No traceback for the user: it could carry request details. The log gets a redacted line.
        log.error("Unexpected error: %s: %s", type(exc).__name__, redact(str(exc)))
        typer.echo("Error: something went wrong. No result was produced.", err=True)
        raise typer.Exit(EXIT_UNEXPECTED) from None

    if as_json:
        _print_json(report.to_dict(), explanation.internal, explanation.client)
    else:
        if not client_only:
            _print_summary(explanation.internal, _STATUS_COLORS[report.status])
        if not summary_only:
            _print_client(explanation.client, lang)
    raise typer.Exit(_exit_code(report))


@app.command("deposit", epilog=DEPOSIT_EPILOG)
def deposit_command(
    hash_or_url: Annotated[
        str, typer.Argument(metavar="HASH_OR_URL", help="Transaction hash or explorer link.")
    ],
    address: Annotated[str, typer.Option(help="Expected receiving address.")],
    amount: Annotated[str, typer.Option(help="Expected amount, for example 100 or 12.5.")],
    asset: Annotated[str, typer.Option(help="Expected asset, for example USDT-TRON or ETH.")],
    lang: Annotated[Lang, typer.Option(help="Language of the client message.")] = Lang.EN,
    confirmations: Annotated[
        int | None,
        typer.Option(min=1, help="Confirmations required to call a transaction final."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the result and the texts as JSON and nothing else."),
    ] = False,
    client_only: Annotated[
        bool, typer.Option("--client-only", help="Print only the message for the client.")
    ] = False,
    summary_only: Annotated[
        bool, typer.Option("--summary-only", help="Print only the internal summary.")
    ] = False,
) -> None:
    """Check a deposit: does a transaction match the expected asset, address and amount?

    The asset sets the expected network. If the hash is not found there, the other networks
    are searched, so a transfer on the wrong network is reported as such. Verdicts: MATCH,
    WAITING (it matches but is not final yet), MISMATCH (with the reasons) and INCONCLUSIVE
    (nothing to compare, for example not found). The headings go to stderr and the texts to
    stdout, so a pipe gets only the text.
    """
    if sum([as_json, client_only, summary_only]) > 1:
        raise typer.BadParameter("Use only one of --json, --client-only and --summary-only.")

    try:
        expected = parse_expected(address, amount, asset)
    except DepositInputError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID_INPUT) from None

    try:
        result = find_deposit(hash_or_url, expected, required_confirmations=confirmations)
        explanation = explain_deposit(result, lang.value)
    except MissingApiKeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(EXIT_NO_API_KEY) from None
    except Exception as exc:
        log.error("Unexpected error: %s: %s", type(exc).__name__, redact(str(exc)))
        typer.echo("Error: something went wrong. No result was produced.", err=True)
        raise typer.Exit(EXIT_UNEXPECTED) from None

    if as_json:
        _print_json(result.to_dict(), explanation.internal, explanation.client)
    else:
        if not client_only:
            _print_summary(explanation.internal, _VERDICT_COLORS[result.verdict])
        if not summary_only:
            _print_client(explanation.client, lang)
    raise typer.Exit(_deposit_exit_code(result))


def _deposit_exit_code(result: DepositResult) -> int:
    match result.verdict:
        case Verdict.MATCH:
            return EXIT_FOUND
        case Verdict.MISMATCH:
            return EXIT_MISMATCH
        case Verdict.WAITING:
            return EXIT_WAITING
    # Inconclusive: the code says why. A transaction that is not in a block yet is a wait.
    if result.report.status is Status.PENDING:
        return EXIT_WAITING
    return _exit_code(result.report)


def _exit_code(report: TxReport) -> int:
    match report.status:
        case Status.NOT_FOUND:
            return EXIT_NOT_FOUND
        case Status.INVALID_INPUT:
            return EXIT_INVALID_INPUT
        case Status.PROVIDER_ERROR:
            return EXIT_PROVIDER_ERROR
    return EXIT_FOUND


def _print_json(data: dict[str, Any], internal: str, client: str) -> None:
    data["explanation"] = {"internal": internal, "client": client}
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _print_summary(internal: str, color: str) -> None:
    """The internal summary may use colors. Click drops them when the output is not a terminal."""
    typer.echo("Internal summary", err=True)
    for number, line in enumerate(internal.splitlines()):
        if number == 0:
            style: dict[str, object] = {"fg": color, "bold": True}
        elif line.startswith("Notes:"):
            style = {"fg": "yellow"}
        else:
            style = {}
        typer.secho(line, color=_color(), **style)  # type: ignore[arg-type]


def _print_client(client: str, lang: Lang) -> None:
    """Plain text only: no colors, borders or symbols, so it can be copied as it is."""
    typer.echo(f"\nMessage for the client ({lang.value})", err=True)
    typer.echo(client)


def _color() -> bool | None:
    # None lets Click decide: colors on a terminal, none in a pipe.
    return False if os.environ.get("NO_COLOR") else None
