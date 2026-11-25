import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tx_checker.analyze.evm import analyze_evm
from tx_checker.analyze.tron import analyze_tron
from tx_checker.config import CHAINS, MissingApiKeyError, get_chain
from tx_checker.deposit_check import (
    ASSET_NETWORKS,
    DepositInputError,
    DepositResult,
    Expected,
    Reason,
    Verdict,
    check_deposit,
    find_deposit,
    parse_address,
    parse_amount,
    parse_asset,
    parse_expected,
    supported_assets,
)
from tx_checker.models import (
    Network,
    ReportWarning,
    Status,
    TokenTransfer,
    TxReport,
    WarningCode,
)
from tx_checker.providers.etherscan import BASE_URL, EtherscanClient

FIXTURES = Path(__file__).parent / "fixtures"
HASH = "ab" * 32
EVM_HASH = f"0x{HASH}"

TRON_RECIPIENT = "THobV9PPMgzxJtrHAUNLJ88CWWLeJF5iNb"
TRON_OTHER = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_LEGACY_USDC = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
ETH_RECIPIENT = "0x59302f15c6c604d3f43d2902337444388119a1d1"
ETH_OTHER = "0x1111111111111111111111111111111111111111"
ETH_USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
POLYGON_USDT0 = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
ARBITRUM_USD0 = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"


def tron_report(name: str) -> TxReport:
    return analyze_tron(json.loads((FIXTURES / "tron" / f"{name}.json").read_text("utf-8")))


def evm_report(name: str) -> TxReport:
    data = json.loads((FIXTURES / "evm" / f"{name}.json").read_text("utf-8"))
    responses = data["responses"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.params["action"]])

    with respx.mock(assert_all_called=False) as router:
        router.get(BASE_URL).mock(side_effect=handler)
        client = EtherscanClient("TESTKEY", http=httpx.Client(), sleep=lambda _: None)
        raw = client.fetch(data["_meta"]["tx_hash"], data["_meta"]["chain_id"])
    assert raw is not None
    return analyze_evm(raw, get_chain(data["_meta"]["chain_id"]))


def sent(result: DepositResult) -> list[tuple[str, str | None]]:
    return [(format(item.amount, "f"), item.symbol) for item in result.sent_instead]


def expect(asset: str, address: str, amount: str) -> Expected:
    return parse_expected(address, amount, asset)


def transfer(contract: str, to: str, amount: str, symbol: str | None = None) -> TokenTransfer:
    return TokenTransfer(
        token_contract=contract,
        from_address="x",
        to_address=to,
        amount=Decimal(amount),
        token_symbol=symbol,
        decimals=6,
    )


def tron_with(*transfers: TokenTransfer, **fields: Any) -> TxReport:
    base: dict[str, Any] = {
        "hash": HASH,
        "status": Status.SUCCESS,
        "network": Network.TRON,
        "chain_name": "TRON",
        "confirmations": 100,
        "required_confirmations": 19,
        "token_transfers": list(transfers),
    }
    return TxReport(**{**base, **fields})


def evm_with(chain_id: int, *transfers: TokenTransfer, **fields: Any) -> TxReport:
    base: dict[str, Any] = {
        "hash": EVM_HASH,
        "status": Status.SUCCESS,
        "network": Network.EVM,
        "chain_id": chain_id,
        "chain_name": CHAINS[chain_id].name,
        "native_symbol": CHAINS[chain_id].native_symbol,
        "confirmations": 100,
        "required_confirmations": 12,
        "token_transfers": list(transfers),
    }
    return TxReport(**{**base, **fields})


# assets


@pytest.mark.parametrize(
    ("text", "name", "network", "chain_id", "native"),
    [
        ("USDT-TRON", "USDT-TRON", Network.TRON, None, False),
        ("usdc-tron", "USDC-TRON", Network.TRON, None, False),
        ("USDT-TRC20", "USDT-TRON", Network.TRON, None, False),
        ("USDC-TRC20", "USDC-TRON", Network.TRON, None, False),
        ("USDT-ETHEREUM", "USDT-ETHEREUM", Network.EVM, 1, False),
        ("USDT-ERC20", "USDT-ETHEREUM", Network.EVM, 1, False),
        ("usdc-erc20", "USDC-ETHEREUM", Network.EVM, 1, False),
        ("USDT-POLYGON", "USDT-POLYGON", Network.EVM, 137, False),
        ("USDC-ARBITRUM", "USDC-ARBITRUM", Network.EVM, 42161, False),
        ("ETH", "ETH", Network.EVM, 1, True),
        ("eth", "ETH", Network.EVM, 1, True),
        ("TRX", "TRX", Network.TRON, None, True),
        ("POL", "POL", Network.EVM, 137, True),
        ("  USDT-TRON  ", "USDT-TRON", Network.TRON, None, False),
    ],
)
def test_parse_asset(
    text: str, name: str, network: Network, chain_id: int | None, native: bool
) -> None:
    asset = parse_asset(text)
    assert (asset.name, asset.network, asset.chain_id, asset.native) == (
        name,
        network,
        chain_id,
        native,
    )


@pytest.mark.parametrize(
    "text", ["", "USDT", "BTC", "USDT-BITCOIN", "ETH-ETHEREUM", "TRX-TRON", "DAI-TRON", "-TRON"]
)
def test_parse_asset_rejects_unknown(text: str) -> None:
    with pytest.raises(DepositInputError, match="Supported assets"):
        parse_asset(text)


def test_unknown_asset_message_is_short_for_long_input() -> None:
    with pytest.raises(DepositInputError) as exc:
        parse_asset("X" * 500)
    assert len(str(exc.value)) < 300


def test_supported_assets_cover_every_network() -> None:
    names = supported_assets()
    assert names[:2] == ["USDT-TRON", "USDC-TRON"]
    assert names[-3:] == ["ETH", "TRX", "POL"]
    assert len(names) == 2 * len(ASSET_NETWORKS) + 3
    assert all(parse_asset(name).name == name for name in names)


def test_asset_chains_are_free_tier_mainnets() -> None:
    for network, chain_id in ASSET_NETWORKS.values():
        if network is Network.EVM:
            assert chain_id in CHAINS
            assert CHAINS[chain_id].free_tier
            assert not CHAINS[chain_id].testnet


# addresses and amounts


@pytest.mark.parametrize("address", [TRON_RECIPIENT, TRON_USDT, TRON_LEGACY_USDC])
def test_tron_addresses_pass(address: str) -> None:
    assert parse_address(f"  {address} ", Network.TRON) == address


def test_tron_address_with_a_typo_fails_the_checksum() -> None:
    typo = TRON_RECIPIENT[:10] + ("A" if TRON_RECIPIENT[10] != "A" else "B") + TRON_RECIPIENT[11:]
    with pytest.raises(DepositInputError, match="TRON address"):
        parse_address(typo, Network.TRON)


@pytest.mark.parametrize("address", ["", "T123", ETH_RECIPIENT, TRON_RECIPIENT + "x"])
def test_bad_tron_addresses(address: str) -> None:
    with pytest.raises(DepositInputError, match="TRON address"):
        parse_address(address, Network.TRON)


def test_evm_address_is_checked_by_shape_only() -> None:
    assert parse_address(ETH_RECIPIENT.upper().replace("0X", "0x"), Network.EVM)
    assert parse_address(ETH_RECIPIENT, Network.EVM) == ETH_RECIPIENT


@pytest.mark.parametrize(
    "address", ["", "0x123", ETH_RECIPIENT[2:], ETH_RECIPIENT + "0", TRON_RECIPIENT]
)
def test_bad_evm_addresses(address: str) -> None:
    with pytest.raises(DepositInputError, match="EVM address"):
        parse_address(address, Network.EVM)


def test_address_for_the_wrong_family_is_rejected_by_parse_expected() -> None:
    with pytest.raises(DepositInputError, match="TRON address"):
        parse_expected(ETH_RECIPIENT, "1", "USDT-TRON")
    with pytest.raises(DepositInputError, match="EVM address"):
        parse_expected(TRON_RECIPIENT, "1", "USDT-ETHEREUM")


@pytest.mark.parametrize(
    ("text", "value"),
    [("100", "100"), ("12.5", "12.5"), (" 0.000001 ", "0.000001"), ("1529.315000", "1529.315")],
)
def test_parse_amount(text: str, value: str) -> None:
    assert parse_amount(text) == Decimal(value)


@pytest.mark.parametrize(
    "text", ["", "0", "0.0", "-1", "1,5", "1e3", "abc", "1.", ".5", "NaN", "Infinity", "1 000"]
)
def test_bad_amounts(text: str) -> None:
    with pytest.raises(DepositInputError, match="positive number"):
        parse_amount(text)


def test_amount_is_a_decimal_not_a_float() -> None:
    assert isinstance(parse_amount("0.1"), Decimal)
    assert parse_amount("0.1") + parse_amount("0.2") == Decimal("0.3")


def test_parse_expected_reports_the_asset_first() -> None:
    with pytest.raises(DepositInputError, match="Unknown asset"):
        parse_expected("bad", "bad", "bad")


# comparison on recorded transactions


def test_tron_usdt_match() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "1529.315")
    )
    assert result.verdict is Verdict.MATCH
    assert result.reasons == []
    assert result.actual_amount == Decimal("1529.315")
    assert result.sent_as is None
    assert not result.similar_address


def test_alias_and_trailing_zeros_match() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("usdt-trc20", TRON_RECIPIENT, "1529.315000")
    )
    assert result.verdict is Verdict.MATCH


def test_tron_amount_lower_shows_both_amounts() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "2000")
    )
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.AMOUNT_LOWER]
    assert result.actual_amount == Decimal("1529.315")
    assert result.expected.amount == Decimal("2000")


def test_tron_amount_higher() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "1000")
    )
    assert result.reasons == [Reason.AMOUNT_HIGHER]
    assert result.actual_amount == Decimal("1529.315")


def test_one_unit_difference_is_not_hidden() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "1529.316")
    )
    assert result.reasons == [Reason.AMOUNT_LOWER]


def test_tron_wrong_address() -> None:
    other = "TKgmbuXFYn58oCrFJFFDgneWc9aRh9eSkz"
    result = check_deposit(tron_report("usdt_transfer"), expect("USDT-TRON", other, "1529.315"))
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_ADDRESS]
    assert result.actual_recipient == TRON_RECIPIENT
    assert result.actual_amount is None
    assert not result.similar_address


def test_tron_address_comparison_is_case_sensitive() -> None:
    swapped = TRON_RECIPIENT.swapcase()
    expected = Expected(parse_asset("USDT-TRON"), swapped, Decimal("1529.315"))
    result = check_deposit(tron_report("usdt_transfer"), expected)
    assert result.reasons == [Reason.WRONG_ADDRESS]


def test_tron_wrong_token() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDC-TRON", TRON_RECIPIENT, "1529.315")
    )
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("1529.315", "USDT")]


def test_tron_usdt_for_an_expected_trx() -> None:
    result = check_deposit(tron_report("usdt_transfer"), expect("TRX", TRON_RECIPIENT, "5"))
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("1529.315", "USDT")]


def test_trx_match_and_wrong_token_for_a_trx_transfer() -> None:
    report = tron_report("trx_transfer")
    address = report.to_address or ""
    assert check_deposit(report, expect("TRX", address, "7")).verdict is Verdict.MATCH
    wrong = check_deposit(report, expect("USDT-TRON", address, "7"))
    assert wrong.reasons == [Reason.WRONG_TOKEN]
    assert sent(wrong) == [("7", "TRX")]


def test_failed_transaction_is_a_mismatch_with_one_reason() -> None:
    result = check_deposit(
        tron_report("failed_out_of_energy"),
        expect("USDT-TRON", "TJVKZXswjk77V3DTzTv1uvSYCyz3cVR5iK", "22"),
    )
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.TX_FAILED]
    assert result.actual_amount is None


def test_not_final_but_matching_is_waiting() -> None:
    report = tron_report("unconfirmed_trx")
    result = check_deposit(report, expect("TRX", report.to_address or "", "0.000006"))
    assert result.verdict is Verdict.WAITING
    assert result.reasons == []
    assert result.actual_amount == Decimal("0.000006")
    assert any(w.code is WarningCode.LOW_CONFIRMATIONS for w in result.report.warnings)


def test_not_final_with_a_wrong_amount_is_a_mismatch_not_waiting() -> None:
    report = tron_report("unconfirmed_trx")
    result = check_deposit(report, expect("TRX", report.to_address or "", "1"))
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.AMOUNT_LOWER]


def test_tron_report_against_an_evm_asset_is_wrong_network() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"), expect("USDT-ETHEREUM", ETH_RECIPIENT, "1529.315")
    )
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_NETWORK]
    assert result.found_on == "TRON"
    assert result.actual_amount is None


def test_eth_match_ignores_address_case() -> None:
    report = evm_report("eth_transfer")
    upper = "0x" + ETH_RECIPIENT[2:].upper()
    result = check_deposit(report, expect("ETH", upper, "0.08"))
    assert result.verdict is Verdict.MATCH


def test_eth_amount_and_address_mismatches() -> None:
    report = evm_report("eth_transfer")
    assert check_deposit(report, expect("ETH", ETH_RECIPIENT, "0.1")).reasons == [
        Reason.AMOUNT_LOWER
    ]
    assert check_deposit(report, expect("ETH", ETH_RECIPIENT, "0.05")).reasons == [
        Reason.AMOUNT_HIGHER
    ]
    wrong = check_deposit(report, expect("ETH", ETH_OTHER, "0.08"))
    assert wrong.reasons == [Reason.WRONG_ADDRESS]
    assert wrong.actual_recipient == ETH_RECIPIENT


def test_eth_for_an_expected_usdt_is_wrong_token() -> None:
    result = check_deposit(
        evm_report("eth_transfer"), expect("USDT-ETHEREUM", ETH_RECIPIENT, "0.08")
    )
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("0.08", "ETH")]


def test_evm_usdt_transfer_matches_then_differs_by_token_and_chain() -> None:
    report = evm_report("usdt_transfer")
    transfer_ = report.token_transfers[0]
    address, amount = transfer_.to_address, format(transfer_.amount, "f")
    assert check_deposit(report, expect("USDT-ERC20", address, amount)).verdict is Verdict.MATCH
    assert check_deposit(report, expect("USDC-ETHEREUM", address, amount)).reasons == [
        Reason.WRONG_TOKEN
    ]
    polygon = check_deposit(report, expect("USDT-POLYGON", address, amount))
    assert polygon.reasons == [Reason.WRONG_NETWORK]
    assert polygon.found_on == "Ethereum"


def test_evm_failed_transactions() -> None:
    for name in ("failed_out_of_gas", "failed_reverted"):
        result = check_deposit(evm_report(name), expect("ETH", ETH_OTHER, "1"))
        assert result.verdict is Verdict.MISMATCH
        assert result.reasons == [Reason.TX_FAILED]


def test_pending_evm_is_inconclusive_on_the_expected_chain() -> None:
    result = check_deposit(evm_report("pending"), expect("ETH", ETH_OTHER, "1"))
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.reasons == []


def test_pending_on_another_chain_is_still_a_wrong_network() -> None:
    report = replace(evm_report("pending"), chain_id=137, chain_name="Polygon")
    result = check_deposit(report, expect("ETH", ETH_OTHER, "1"))
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_NETWORK]
    assert result.found_on == "Polygon"


def test_nothing_to_compare_is_inconclusive() -> None:
    for status in (Status.NOT_FOUND, Status.PROVIDER_ERROR, Status.INVALID_INPUT):
        report = TxReport(hash=HASH, status=status, network=Network.TRON, chain_name="TRON")
        result = check_deposit(report, expect("TRX", TRON_RECIPIENT, "1"))
        assert result.verdict is Verdict.INCONCLUSIVE
        assert result.reasons == []


# token matching goes through the registry


def test_usdt0_on_polygon_counts_as_usdt_and_is_named() -> None:
    report = evm_with(137, transfer(POLYGON_USDT0, ETH_RECIPIENT, "50", symbol="USDT"))
    result = check_deposit(report, expect("USDT-POLYGON", ETH_RECIPIENT, "50"))
    assert result.verdict is Verdict.MATCH
    assert result.sent_as == "USDT0"


def test_usd0_on_arbitrum_counts_as_usdt() -> None:
    report = evm_with(42161, transfer(ARBITRUM_USD0, ETH_RECIPIENT, "50"))
    result = check_deposit(report, expect("USDT-ARBITRUM", ETH_RECIPIENT, "50"))
    assert result.verdict is Verdict.MATCH
    assert result.sent_as == "USD₮0"


def test_usdc_is_not_usdt0() -> None:
    report = evm_with(137, transfer(POLYGON_USDT0, ETH_RECIPIENT, "50"))
    result = check_deposit(report, expect("USDC-POLYGON", ETH_RECIPIENT, "50"))
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("50", "USDT0")]


def test_legacy_usdc_on_tron_counts_as_usdc_and_keeps_its_warning() -> None:
    warning = ReportWarning(WarningCode.LEGACY_TOKEN, "older version")
    report = tron_with(transfer(TRON_LEGACY_USDC, TRON_RECIPIENT, "30"), warnings=[warning])
    result = check_deposit(report, expect("USDC-TRON", TRON_RECIPIENT, "30"))
    assert result.verdict is Verdict.MATCH
    assert result.sent_as == "USDC (legacy)"
    assert result.report.warnings == [warning]


def test_legacy_usdc_is_not_usdt() -> None:
    report = tron_with(transfer(TRON_LEGACY_USDC, TRON_RECIPIENT, "30"))
    result = check_deposit(report, expect("USDT-TRON", TRON_RECIPIENT, "30"))
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("30", "USDC (legacy)")]


def test_a_token_that_calls_itself_usdt_is_not_usdt() -> None:
    fake = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
    report = tron_with(transfer(fake, TRON_RECIPIENT, "30", symbol="USDT"))
    result = check_deposit(report, expect("USDT-TRON", TRON_RECIPIENT, "30"))
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("30", None)]
    assert result.sent_instead[0].contract == fake


def test_unknown_evm_token_is_wrong_token() -> None:
    report = evm_with(1, transfer(ETH_OTHER, ETH_RECIPIENT, "5000000"))
    result = check_deposit(report, expect("USDT-ETHEREUM", ETH_RECIPIENT, "5"))
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("5000000", None)]


def test_same_contract_on_another_chain_is_not_matched() -> None:
    # the Ethereum USDT contract address means nothing on Polygon
    report = evm_with(137, transfer(ETH_USDT, ETH_RECIPIENT, "50"))
    result = check_deposit(report, expect("USDT-POLYGON", ETH_RECIPIENT, "50"))
    assert result.reasons == [Reason.WRONG_TOKEN]


def test_transfers_to_the_expected_address_are_added_up() -> None:
    report = tron_with(
        transfer(TRON_USDT, TRON_RECIPIENT, "10.5"),
        transfer(TRON_USDT, TRON_OTHER, "99"),
        transfer(TRON_USDT, TRON_RECIPIENT, "4.5"),
    )
    result = check_deposit(report, expect("USDT-TRON", TRON_RECIPIENT, "15"))
    assert result.verdict is Verdict.MATCH
    assert result.actual_amount == Decimal("15")


def test_other_transfers_of_the_asset_do_not_hide_a_wrong_address() -> None:
    report = tron_with(transfer(TRON_USDT, TRON_OTHER, "15"))
    result = check_deposit(report, expect("USDT-TRON", TRON_RECIPIENT, "15"))
    assert result.reasons == [Reason.WRONG_ADDRESS]
    assert result.actual_recipient == TRON_OTHER


# native coins


def test_native_value_zero_with_no_transfers_is_an_amount_problem() -> None:
    report = evm_with(1, native_amount=Decimal(0), to_address=ETH_RECIPIENT)
    result = check_deposit(report, expect("ETH", ETH_RECIPIENT, "1"))
    assert result.reasons == [Reason.AMOUNT_LOWER]
    assert result.actual_amount == Decimal(0)


def test_native_value_zero_with_a_token_transfer_is_wrong_token() -> None:
    report = evm_with(
        1,
        transfer(ETH_USDT, ETH_RECIPIENT, "5"),
        native_amount=Decimal(0),
        to_address=ETH_USDT,
    )
    result = check_deposit(report, expect("ETH", ETH_RECIPIENT, "1"))
    assert result.reasons == [Reason.WRONG_TOKEN]
    assert sent(result) == [("5", "USDT")]


def test_native_pol_on_polygon() -> None:
    report = evm_with(137, native_amount=Decimal("12.5"), to_address=ETH_RECIPIENT)
    assert check_deposit(report, expect("POL", ETH_RECIPIENT, "12.5")).verdict is Verdict.MATCH
    on_ethereum = check_deposit(report, expect("ETH", ETH_RECIPIENT, "12.5"))
    assert on_ethereum.reasons == [Reason.WRONG_NETWORK]
    assert on_ethereum.found_on == "Polygon"


def test_native_send_without_a_recipient_is_a_wrong_address() -> None:
    report = evm_with(1, native_amount=Decimal(1), to_address=None)
    result = check_deposit(report, expect("ETH", ETH_RECIPIENT, "1"))
    assert result.reasons == [Reason.WRONG_ADDRESS]
    assert result.actual_recipient is None


# address poisoning


def test_similar_tron_address_is_flagged() -> None:
    lookalike = TRON_RECIPIENT[:4] + "A" * 26 + TRON_RECIPIENT[-4:]
    expected = Expected(parse_asset("USDT-TRON"), lookalike, Decimal("1529.315"))
    result = check_deposit(tron_report("usdt_transfer"), expected)
    assert result.reasons == [Reason.WRONG_ADDRESS]
    assert result.similar_address


def test_similar_evm_address_compares_after_the_prefix() -> None:
    lookalike = "0x5930" + "0" * 32 + "a1d1"
    report = evm_report("eth_transfer")
    result = check_deposit(report, expect("ETH", lookalike, "0.08"))
    assert result.reasons == [Reason.WRONG_ADDRESS]
    assert result.similar_address
    # only the 0x and one more character match: not similar
    result = check_deposit(report, expect("ETH", "0x5" + "0" * 35 + "a1d1", "0.08"))
    assert not result.similar_address


def test_only_the_first_four_matching_is_not_similar() -> None:
    different_end = TRON_RECIPIENT[:4] + "A" * 26 + "zzzz"
    expected = Expected(parse_asset("USDT-TRON"), different_end, Decimal("1529.315"))
    assert not check_deposit(tron_report("usdt_transfer"), expected).similar_address


def test_an_unrelated_address_is_not_similar() -> None:
    result = check_deposit(
        tron_report("usdt_transfer"),
        expect("USDT-TRON", "TKgmbuXFYn58oCrFJFFDgneWc9aRh9eSkz", "1529.315"),
    )
    assert not result.similar_address


# the search


class Lookup:
    """A scripted `check`: answers by (network, chain id) and records every call."""

    def __init__(self, found: dict[tuple[Network | None, int | None], TxReport] | None = None):
        self.found = found or {}
        self.calls: list[tuple[Network | None, int | None]] = []
        self.required: list[int | None] = []
        self.raise_for: dict[tuple[Network | None, int | None], Exception] = {}

    def __call__(
        self,
        raw_input: str,
        *,
        network: Network | None = None,
        chain_id: int | None = None,
        required_confirmations: int | None = None,
    ) -> TxReport:
        key = (network, chain_id)
        self.calls.append(key)
        self.required.append(required_confirmations)
        if key in self.raise_for:
            raise self.raise_for[key]
        if key in self.found:
            return self.found[key]
        if network is None and chain_id is None:
            return TxReport(hash=raw_input, status=Status.INVALID_INPUT, message="bad")
        name = "TRON" if network is Network.TRON else CHAINS[chain_id or 1].name
        return TxReport(
            hash=raw_input,
            status=Status.NOT_FOUND,
            network=network,
            chain_id=chain_id,
            chain_name=name,
            networks_checked=[name],
        )


EVM = Network.EVM
TRON = Network.TRON


def found_on_polygon() -> TxReport:
    return evm_with(137, native_amount=Decimal("1"), to_address=ETH_RECIPIENT)


def test_found_on_the_expected_chain_needs_one_call() -> None:
    lookup = Lookup({(EVM, 1): evm_report("eth_transfer")})
    result = find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "0.08"), lookup=lookup)
    assert result.verdict is Verdict.MATCH
    assert lookup.calls == [(EVM, 1)]


def test_required_confirmations_are_passed_on() -> None:
    lookup = Lookup({(EVM, 1): evm_report("eth_transfer")})
    find_deposit(
        EVM_HASH, expect("ETH", ETH_RECIPIENT, "0.08"), required_confirmations=7, lookup=lookup
    )
    assert lookup.required == [7]


def test_found_on_another_evm_chain_is_a_wrong_network_naming_it() -> None:
    lookup = Lookup({(EVM, 137): found_on_polygon()})
    result = find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(EVM, 1), (EVM, 137)]
    assert result.verdict is Verdict.MISMATCH
    assert result.reasons == [Reason.WRONG_NETWORK]
    assert result.found_on == "Polygon"


def test_the_expected_chain_is_tried_first() -> None:
    lookup = Lookup()
    find_deposit(EVM_HASH, expect("USDT-ARBITRUM", ETH_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(EVM, 42161), (EVM, 1), (EVM, 137)]


def test_the_search_skips_testnets_and_chains_the_free_plan_does_not_serve() -> None:
    lookup = Lookup()
    find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)
    searched = {chain_id for _, chain_id in lookup.calls}
    assert searched == {1, 137, 42161}
    assert 11155111 not in searched
    assert 56 not in searched


def test_an_evm_hash_never_searches_tron() -> None:
    lookup = Lookup()
    find_deposit(EVM_HASH, expect("USDT-TRON", TRON_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(EVM, 1), (EVM, 137), (EVM, 42161)]


def test_a_bare_hash_for_a_tron_asset_looks_at_tron_first() -> None:
    lookup = Lookup()
    find_deposit(HASH, expect("USDT-TRON", TRON_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(TRON, None), (EVM, 1), (EVM, 137), (EVM, 42161)]


def test_a_bare_hash_for_an_evm_asset_tries_the_expected_chain_then_tron() -> None:
    lookup = Lookup()
    find_deposit(HASH, expect("USDT-POLYGON", ETH_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(EVM, 137), (TRON, None), (EVM, 1), (EVM, 42161)]


def test_a_tron_hash_given_for_an_evm_asset_is_a_wrong_network() -> None:
    lookup = Lookup({(TRON, None): tron_report("usdt_transfer")})
    result = find_deposit(HASH, expect("USDT-ETHEREUM", ETH_RECIPIENT, "1529.315"), lookup=lookup)
    assert lookup.calls == [(EVM, 1), (TRON, None)]
    assert result.reasons == [Reason.WRONG_NETWORK]
    assert result.found_on == "TRON"


def test_a_tronscan_link_looks_only_at_tron() -> None:
    lookup = Lookup()
    result = find_deposit(
        f"https://tronscan.org/#/transaction/{HASH}",
        expect("USDT-ETHEREUM", ETH_RECIPIENT, "1"),
        lookup=lookup,
    )
    assert lookup.calls == [(TRON, None)]
    assert result.verdict is Verdict.INCONCLUSIVE


def test_the_chain_of_an_explorer_link_is_tried_before_the_expected_one() -> None:
    lookup = Lookup({(EVM, 137): found_on_polygon()})
    result = find_deposit(
        f"https://polygonscan.com/tx/{EVM_HASH}", expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup
    )
    assert lookup.calls == [(EVM, 137)]
    assert result.reasons == [Reason.WRONG_NETWORK]


def test_a_link_to_the_expected_chain_adds_no_extra_lookup() -> None:
    lookup = Lookup({(EVM, 1): evm_report("eth_transfer")})
    find_deposit(
        f"https://etherscan.io/tx/{EVM_HASH}", expect("ETH", ETH_RECIPIENT, "0.08"), lookup=lookup
    )
    assert lookup.calls == [(EVM, 1)]


def test_not_found_anywhere_lists_every_place_that_was_checked() -> None:
    lookup = Lookup()
    result = find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.report.status is Status.NOT_FOUND
    assert result.report.networks_checked == ["Ethereum", "Polygon", "Arbitrum One"]
    assert (
        result.report.message
        == "Not found on Ethereum, Polygon, Arbitrum One. All of them were checked."
    )


def test_a_provider_error_stops_the_search() -> None:
    error = TxReport(
        hash=EVM_HASH,
        status=Status.PROVIDER_ERROR,
        network=EVM,
        chain_id=1,
        chain_name="Ethereum",
        message="limit reached",
    )
    lookup = Lookup({(EVM, 1): error})
    result = find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(EVM, 1)]
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.report.status is Status.PROVIDER_ERROR


def test_invalid_input_costs_one_lookup_and_is_inconclusive() -> None:
    lookup = Lookup()
    result = find_deposit("nonsense", expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(None, None)]
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.report.status is Status.INVALID_INPUT


def test_a_missing_key_for_the_expected_network_is_raised() -> None:
    lookup = Lookup()
    lookup.raise_for[(EVM, 1)] = MissingApiKeyError("no key")
    with pytest.raises(MissingApiKeyError):
        find_deposit(EVM_HASH, expect("ETH", ETH_RECIPIENT, "1"), lookup=lookup)


def test_a_missing_key_only_stops_the_extra_search() -> None:
    lookup = Lookup()
    lookup.raise_for[(EVM, 1)] = MissingApiKeyError("no key")
    result = find_deposit(HASH, expect("USDT-TRON", TRON_RECIPIENT, "1"), lookup=lookup)
    assert lookup.calls == [(TRON, None), (EVM, 1)]
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.report.networks_checked == ["TRON"]
    assert "no Etherscan key" in (result.report.message or "")


def test_a_hit_on_tron_for_a_tron_asset_is_compared_directly() -> None:
    lookup = Lookup({(TRON, None): tron_report("usdt_transfer")})
    result = find_deposit(HASH, expect("USDT-TRON", TRON_RECIPIENT, "1529.315"), lookup=lookup)
    assert result.verdict is Verdict.MATCH
    assert lookup.calls == [(TRON, None)]
