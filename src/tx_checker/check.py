import logging

from tx_checker.analyze.evm import analyze_evm
from tx_checker.analyze.tron import analyze_tron
from tx_checker.config import (
    DEFAULT_CHAIN_ID,
    TRON_NAME,
    Chain,
    MissingApiKeyError,
    UnknownChainError,
    get_chain,
    load_etherscan_key,
    load_tronscan_key,
    tron_tx_url,
)
from tx_checker.detect import Detection, detect
from tx_checker.models import Network, ReportWarning, Status, TxReport, WarningCode
from tx_checker.providers import ProviderError
from tx_checker.providers.etherscan import EtherscanClient
from tx_checker.providers.tronscan import TronscanClient
from tx_checker.redact import redact

log = logging.getLogger(__name__)

MAX_ECHO = 100


def check(
    raw_input: str,
    *,
    network: Network | None = None,
    chain_id: int | None = None,
    required_confirmations: int | None = None,
    etherscan: EtherscanClient | None = None,
    tronscan: TronscanClient | None = None,
) -> TxReport:
    """Check one transaction. Bad input and provider problems come back as a report.

    A missing API key raises MissingApiKeyError: it is a setup problem, not a result.
    """
    if chain_id is not None:
        if network is Network.TRON:
            return _invalid(raw_input, "A chain id only applies to EVM networks.")
        network = Network.EVM
    detection = detect(raw_input, network)
    if not detection.ok or detection.tx_hash is None:
        return _invalid(raw_input, detection.error or "Unrecognized input.")

    clients = _Clients(etherscan, tronscan)
    try:
        if detection.network is Network.TRON:
            return _check_tron(detection, required_confirmations, clients)
        return _check_evm(detection, chain_id, required_confirmations, clients)
    finally:
        clients.close()


class _Clients:
    """Provider clients, created on first use so each key is only needed when its network is."""

    def __init__(self, etherscan: EtherscanClient | None, tronscan: TronscanClient | None) -> None:
        self._etherscan = etherscan
        self._tronscan = tronscan
        self._owned: list[EtherscanClient | TronscanClient] = []

    def evm(self) -> EtherscanClient:
        if self._etherscan is None:
            self._etherscan = EtherscanClient(load_etherscan_key())
            self._owned.append(self._etherscan)
        return self._etherscan

    def tron(self) -> TronscanClient:
        if self._tronscan is None:
            self._tronscan = TronscanClient(load_tronscan_key())
            self._owned.append(self._tronscan)
        return self._tronscan

    def close(self) -> None:
        for client in self._owned:
            client.close()


def _check_tron(detection: Detection, required: int | None, clients: _Clients) -> TxReport:
    tx_hash = str(detection.tx_hash)
    try:
        info = clients.tron().fetch(tx_hash)
    except ProviderError as exc:
        return _provider_error(tx_hash, exc, Network.TRON, explorer_url=tron_tx_url(tx_hash))
    if info is not None:
        return analyze_tron(info, required)

    not_found = TxReport(
        hash=tx_hash,
        status=Status.NOT_FOUND,
        network=Network.TRON,
        chain_name=TRON_NAME,
        explorer_url=tron_tx_url(tx_hash),
        networks_checked=[TRON_NAME],
    )
    if not detection.guessed:
        return not_found

    # A bare 64-hex hash is the normal TRON format, so only an EVM hit counts as a guess.
    chain = get_chain(DEFAULT_CHAIN_ID)
    try:
        evm_report = _lookup_evm(f"0x{tx_hash}", chain, required, clients)
    except MissingApiKeyError:
        # Only here is a missing key soft: the user asked about TRON, the EVM check was our guess.
        not_found.message = (
            f"Not found on {TRON_NAME}. {chain.name} was not checked because no Etherscan key "
            "is configured."
        )
        return not_found
    if evm_report.status is Status.NOT_FOUND:
        not_found.message = f"Not found on {TRON_NAME} or on {chain.name}. Both were checked."
        not_found.networks_checked = [TRON_NAME, chain.name]
        return not_found
    if evm_report.status is Status.PROVIDER_ERROR:
        evm_report.message = f"Not found on {TRON_NAME}. The {chain.name} check failed: " + str(
            evm_report.message
        )
        return evm_report
    evm_report.warnings.insert(
        0,
        ReportWarning(
            WarningCode.NETWORK_GUESSED,
            f"The hash was not found on {TRON_NAME}, so it was looked up as an EVM transaction "
            f"on {chain.name}.",
        ),
    )
    return evm_report


def _check_evm(
    detection: Detection, chain_id: int | None, required: int | None, clients: _Clients
) -> TxReport:
    tx_hash = str(detection.tx_hash)
    # An explicit chain id beats the one from the link, which beats the default.
    chosen = next((c for c in (chain_id, detection.chain_id) if c is not None), DEFAULT_CHAIN_ID)
    try:
        chain = get_chain(chosen)
    except UnknownChainError as exc:
        return _invalid(tx_hash, str(exc))

    report = _lookup_evm(tx_hash, chain, required, clients)
    if detection.chain_unconfirmed and chain_id is None:
        report.warnings.insert(
            0,
            ReportWarning(
                WarningCode.NETWORK_UNCONFIRMED,
                f"The link comes from an explorer this tool does not know, so the chain could "
                f"not be confirmed. {chain.name} was used.",
            ),
        )
    return report


def _lookup_evm(tx_hash: str, chain: Chain, required: int | None, clients: _Clients) -> TxReport:
    try:
        raw = clients.evm().fetch(tx_hash, chain.chain_id)
    except ProviderError as exc:
        return _provider_error(
            tx_hash, exc, Network.EVM, chain=chain, explorer_url=chain.tx_url(tx_hash)
        )
    if raw is None:
        return TxReport(
            hash=tx_hash,
            status=Status.NOT_FOUND,
            network=Network.EVM,
            chain_id=chain.chain_id,
            chain_name=chain.name,
            explorer_url=chain.tx_url(tx_hash),
            networks_checked=[chain.name],
        )
    return analyze_evm(raw, chain, required)


def _provider_error(
    tx_hash: str,
    error: ProviderError,
    network: Network,
    *,
    explorer_url: str,
    chain: Chain | None = None,
) -> TxReport:
    # The message is already redacted. Details stay in the server log.
    log.warning("provider error for %s on %s: %s", tx_hash, network.value, error)
    return TxReport(
        hash=tx_hash,
        status=Status.PROVIDER_ERROR,
        network=network,
        chain_id=chain.chain_id if chain else None,
        chain_name=chain.name if chain else TRON_NAME,
        explorer_url=explorer_url,
        message=str(error),
    )


def _invalid(raw_input: str, message: str) -> TxReport:
    echoed = redact(raw_input.strip())[:MAX_ECHO]
    return TxReport(hash=echoed, status=Status.INVALID_INPUT, message=message)
