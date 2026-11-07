"""The oracle and integration layer — Section 3.14.

This is the module Chapter Three describes and the repository did not have.
Everything above it worked on a projection of the chain; nothing wrote to one.
Three jobs live here:

    1. Writing a risk score on chain, signed by the oracle operator, before any
       deposit can be accepted. Section 3.14.2.
    2. Driving the escrow lifecycle — deposit, confirm, dispute, resolve,
       refund — against EscrowContract.
    3. Reading the contract's events back, so the database is a projection of
       the chain rather than a parallel record that can disagree with it.

Three properties are worth stating plainly, because each is a limitation a
reader should know about rather than discover.

**Custody.** `depositFunds` is payable and must be signed by whoever owns the
money. A server cannot sign for a buyer without holding the buyer's key, and
this service holds one: a single testnet account that funds every escrow on the
buyer's behalf. That is a *demonstration* arrangement, not how an escrow would
custody funds in practice, where the buyer signs in their own wallet. Section
3.15 must say so. The key is a testnet key and holds nothing of value.

**Amount.** The amounts in the interface are notional. What is actually
deposited is `chain_demo_amount_eth`, a few ten-thousandths of a test ether, so
a demonstration does not drain the faucet. The escrow identifier is derived
from the amount actually sent, so the on-chain record is internally consistent;
the notional figure is stored separately and never presented as an on-chain
value.

**Nonce.** `updateRiskScore` rejects any nonce that does not strictly exceed
the last one the oracle accepted, so the nonce cannot be random here. It is
read from the contract and incremented under a lock held for the duration of
the write. That serialises score submission within one process and would not
survive being run as several processes; a production oracle would allocate
nonces from a shared sequence.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Settings

logger = logging.getLogger(__name__)

ARTIFACTS = REPO_ROOT / "contracts" / "artifacts" / "contracts"
DEPLOYMENT_RECORD = REPO_ROOT / "docs" / "evidence" / "deployment.sepolia.json"

# Mirrors contracts/test/helpers.js. The oracle stores it verbatim so a score
# can be traced to the model that produced it.
MODEL_VERSION_BYTES32 = "rf-paysim-v1.0.0"

# How far ahead a score stays valid. The contract rejects an expired one.
ASSESSMENT_TTL_SECONDS = 365 * 24 * 60 * 60

# EscrowContract.State, in declaration order.
CHAIN_STATES = ("INITIATED", "DELIVERED", "DISPUTED", "COMPLETED")

# EscrowContract.Outcome, in declaration order.
CHAIN_OUTCOMES = (
    "NONE", "RELEASED_TO_SELLER", "RESOLVED_FOR_SELLER",
    "RESOLVED_FOR_BUYER", "TIMEOUT_REFUND",
)


class ChainUnavailable(RuntimeError):
    """The chain could not be reached, or is not configured.

    Raised rather than returned so a caller cannot mistake a failed write for a
    successful one. Section 3.15.2 requires the system to fail closed.
    """


@dataclass(frozen=True)
class ChainReceipt:
    """What a confirmed transaction leaves behind, for the audit record."""

    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    explorer: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == 1


@dataclass
class ChainStatus:
    """Whether this layer can be used, and why not when it cannot."""

    configured: bool
    connected: bool = False
    chain_id: int | None = None
    block_number: int | None = None
    escrow_contract: str | None = None
    oracle_contract: str | None = None
    operator: str | None = None
    funder: str | None = None
    funder_balance_eth: float | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _load_abi(name: str) -> list[dict[str, Any]]:
    path = ARTIFACTS / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        raise ChainUnavailable(
            f"No compiled artefact for {name}. Run `npx hardhat compile` in contracts/."
        )
    return json.loads(path.read_text())["abi"]


def _addresses(settings: Settings) -> tuple[str, str]:
    """Contract addresses, from settings when given, else the deploy record.

    The deployment record is the artefact the deploy script writes and the
    report cites, so reading it here keeps one source of truth rather than
    asking an operator to copy two addresses into the environment correctly.
    """
    escrow = settings.escrow_contract_address.strip()
    oracle = settings.risk_oracle_contract_address.strip()
    if escrow and oracle:
        return escrow, oracle

    if not DEPLOYMENT_RECORD.exists():
        raise ChainUnavailable(
            "No contract addresses. Set ESCROW_CONTRACT_ADDRESS and "
            "RISK_ORACLE_CONTRACT_ADDRESS, or deploy with "
            "`npx hardhat run scripts/deploy.js --network sepolia`."
        )
    record = json.loads(DEPLOYMENT_RECORD.read_text())
    return (
        escrow or record["contracts"]["EscrowContract"]["address"],
        oracle or record["contracts"]["RiskOracleContract"]["address"],
    )


class ChainClient:
    """A thin, synchronous client over the two contracts.

    Synchronous on purpose: a Sepolia transaction takes about eleven seconds to
    be included, and hiding that behind an async interface would only move the
    wait. Callers run it off the request path instead.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nonce_lock = threading.Lock()
        self._w3 = None
        self._escrow = None
        self._oracle = None
        self._operator = None
        self._funder = None
        self._init_error: str | None = None
        self._initialise()

    # ------------------------------------------------------------ setup --
    def _initialise(self) -> None:
        s = self._settings
        if not s.chain_enabled:
            self._init_error = "CHAIN_ENABLED is not set"
            return
        if not s.sepolia_rpc_url.strip():
            self._init_error = "SEPOLIA_RPC_URL is empty"
            return

        try:
            from eth_account import Account
            from web3 import Web3
            from web3.middleware import geth_poa_middleware
        except ImportError as error:        # pragma: no cover - install issue
            self._init_error = f"web3 is not installed: {error}"
            return

        try:
            w3 = Web3(Web3.HTTPProvider(
                s.sepolia_rpc_url, request_kwargs={"timeout": 30},
            ))
            # Sepolia is proof-of-stake, but several providers still return
            # extra-data longer than 32 bytes; the middleware tolerates it.
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)

            if not w3.is_connected():
                self._init_error = "the RPC endpoint did not respond"
                return

            escrow_address, oracle_address = _addresses(s)
            self._escrow = w3.eth.contract(
                address=Web3.to_checksum_address(escrow_address),
                abi=_load_abi("EscrowContract"),
            )
            self._oracle = w3.eth.contract(
                address=Web3.to_checksum_address(oracle_address),
                abi=_load_abi("RiskOracleContract"),
            )

            if s.oracle_private_key.strip():
                self._operator = Account.from_key(s.oracle_private_key.strip())
            if s.chain_funder_private_key.strip():
                self._funder = Account.from_key(s.chain_funder_private_key.strip())

            self._w3 = w3
        except ChainUnavailable as error:
            self._init_error = str(error)
        except Exception as error:          # pragma: no cover - env dependent
            self._init_error = f"{type(error).__name__}: {error}"

    # ----------------------------------------------------------- status --
    @property
    def available(self) -> bool:
        return self._w3 is not None and self._operator is not None

    def status(self) -> ChainStatus:
        if self._w3 is None:
            return ChainStatus(
                configured=self._settings.chain_enabled,
                reason=self._init_error or "not initialised",
            )
        try:
            balance = (
                self._w3.eth.get_balance(self._funder.address)
                if self._funder else None
            )
            return ChainStatus(
                configured=True,
                connected=True,
                chain_id=self._w3.eth.chain_id,
                block_number=self._w3.eth.block_number,
                escrow_contract=self._escrow.address,
                oracle_contract=self._oracle.address,
                operator=self._operator.address if self._operator else None,
                funder=self._funder.address if self._funder else None,
                funder_balance_eth=(
                    float(self._w3.from_wei(balance, "ether"))
                    if balance is not None else None
                ),
                reason=None if self._operator else "ORACLE_PRIVATE_KEY is empty",
            )
        except Exception as error:          # pragma: no cover - network
            return ChainStatus(
                configured=True, connected=False,
                reason=f"{type(error).__name__}: {error}",
            )

    def _require(self):
        if self._w3 is None:
            raise ChainUnavailable(self._init_error or "the chain layer is not configured")
        return self._w3

    # ------------------------------------------------------------ reads --
    def compute_escrow_id(
        self, buyer: str, seller: str, amount_wei: int, nonce: int,
    ) -> str:
        """The identifier the contract derives, asked of the contract itself.

        Computing keccak256 here as well would be a second implementation of
        D-003's rule that could drift from the Solidity one. Asking the
        contract costs one `eth_call` and cannot disagree.
        """
        w3 = self._require()
        value = self._escrow.functions.computeEscrowId(
            w3.to_checksum_address(buyer),
            w3.to_checksum_address(seller),
            amount_wei,
            nonce,
        ).call()
        return value.hex() if hasattr(value, "hex") else str(value)

    def get_escrow(self, escrow_id: str) -> dict[str, Any] | None:
        """The contract's own view of one escrow, or None if it has none."""
        w3 = self._require()
        raw = self._escrow.functions.getEscrow(_to_bytes32(w3, escrow_id)).call()
        exists = bool(raw[-1]) if isinstance(raw[-1], bool) else True
        if not exists:
            return None
        return _decode_escrow(raw)

    def has_score(self, escrow_id: str) -> bool:
        w3 = self._require()
        return bool(self._oracle.functions.hasScore(
            _to_bytes32(w3, escrow_id)).call())

    # ----------------------------------------------------------- writes --
    def write_risk_score(
        self, buyer: str, seller: str, amount_wei: int, score_fraction: float,
    ) -> tuple[str, int, ChainReceipt]:
        """Records a score on chain and returns the escrow it belongs to.

        The nonce is allocated and consumed inside one lock: the contract
        requires it to increase strictly, so two concurrent writers reading the
        same `lastNonce` would make the second revert.

        Returns (escrow_id, nonce, receipt).
        """
        w3 = self._require()
        if self._operator is None:
            raise ChainUnavailable("ORACLE_PRIVATE_KEY is empty; cannot sign a score")

        # uint8, and the contract rejects anything above MAX_SCORE.
        score = max(0, min(100, int(round(score_fraction * 100))))
        expiry = int(datetime.now(timezone.utc).timestamp()) + ASSESSMENT_TTL_SECONDS
        model_version = _to_bytes32_text(MODEL_VERSION_BYTES32)

        with self._nonce_lock:
            nonce = int(self._oracle.functions.lastNonce().call()) + 1
            escrow_id = self.compute_escrow_id(buyer, seller, amount_wei, nonce)
            receipt = self._send(
                self._oracle.functions.updateRiskScore(
                    _to_bytes32(w3, escrow_id), score, model_version, nonce, expiry,
                ),
                signer=self._operator,
                label="updateRiskScore",
            )
        return escrow_id, nonce, receipt

    def deposit(
        self, seller: str, arbitrator: str, nonce: int, amount_wei: int,
    ) -> ChainReceipt:
        """Funds the escrow from the demonstration account. See the module note."""
        w3 = self._require()
        funder = self._require_funder()
        return self._send(
            self._escrow.functions.depositFunds(
                w3.to_checksum_address(seller),
                w3.to_checksum_address(arbitrator),
                nonce,
            ),
            signer=funder,
            label="depositFunds",
            value=amount_wei,
        )

    def confirm_delivery(self, escrow_id: str) -> ChainReceipt:
        """Only the buyer may call this, so the funder signs it."""
        w3 = self._require()
        return self._send(
            self._escrow.functions.confirmDelivery(_to_bytes32(w3, escrow_id)),
            signer=self._require_funder(), label="confirmDelivery",
        )

    def initiate_dispute(self, escrow_id: str) -> ChainReceipt:
        w3 = self._require()
        return self._send(
            self._escrow.functions.initiateDispute(_to_bytes32(w3, escrow_id)),
            signer=self._require_funder(), label="initiateDispute",
        )

    def resolve_dispute(self, escrow_id: str, release_to_seller: bool) -> ChainReceipt:
        """Restricted on chain to the escrow's recorded arbitrator.

        The operator account is passed as the arbitrator when an escrow is
        created, so it is the account that can resolve one.
        """
        w3 = self._require()
        if self._operator is None:
            raise ChainUnavailable("ORACLE_PRIVATE_KEY is empty; cannot resolve")
        return self._send(
            self._escrow.functions.resolveDispute(
                _to_bytes32(w3, escrow_id), release_to_seller),
            signer=self._operator, label="resolveDispute",
        )

    def release_funds(self, escrow_id: str) -> ChainReceipt:
        w3 = self._require()
        return self._send(
            self._escrow.functions.releaseFunds(_to_bytes32(w3, escrow_id)),
            signer=self._require_funder(), label="releaseFunds",
        )

    def claim_refund(self, escrow_id: str) -> ChainReceipt:
        w3 = self._require()
        return self._send(
            self._escrow.functions.claimRefund(_to_bytes32(w3, escrow_id)),
            signer=self._require_funder(), label="claimRefund",
        )

    # ----------------------------------------------------------- events --
    def fetch_events(self, from_block: int, to_block: int | str = "latest") -> list[dict]:
        """Every escrow-relevant event in a block range, oldest first.

        Returned as plain dictionaries so the caller does not need web3 types,
        and sorted so a projection built from them cannot apply a later state
        before an earlier one.
        """
        self._require()
        collected: list[dict] = []
        wanted = (
            ("FundsDeposited", self._escrow),
            ("DeliveryConfirmed", self._escrow),
            ("DisputeInitiated", self._escrow),
            ("DisputeResolved", self._escrow),
            ("FundsReleased", self._escrow),
            ("StateChanged", self._escrow),
            ("RiskScoreRecorded", self._oracle),
        )
        for name, contract in wanted:
            try:
                logs = contract.events[name].get_logs(
                    fromBlock=from_block, toBlock=to_block)
            except Exception as error:      # pragma: no cover - provider limits
                logger.warning("could not read %s logs: %s", name, error)
                continue
            for log in logs:
                args = {k: _plain(v) for k, v in dict(log["args"]).items()}
                collected.append({
                    "event": name,
                    "block_number": log["blockNumber"],
                    "log_index": log["logIndex"],
                    "tx_hash": log["transactionHash"].hex(),
                    "args": args,
                })
        collected.sort(key=lambda e: (e["block_number"], e["log_index"]))
        return collected

    def block_number(self) -> int:
        return self._require().eth.block_number

    # --------------------------------------------------------- internals --
    def _require_funder(self):
        if self._funder is None:
            raise ChainUnavailable(
                "CHAIN_FUNDER_PRIVATE_KEY is empty, so no account can sign a "
                "deposit or a buyer action."
            )
        return self._funder

    def _send(self, call, *, signer, label: str, value: int = 0) -> ChainReceipt:
        """Builds, signs, sends and waits for one transaction.

        A reverted transaction is raised rather than returned: the caller is
        recording an escrow's state, and a receipt with status 0 means the
        state did not change.
        """
        w3 = self._require()
        s = self._settings
        try:
            tx = call.build_transaction({
                "from": signer.address,
                "nonce": w3.eth.get_transaction_count(signer.address),
                "value": value,
                "chainId": w3.eth.chain_id,
            })
            signed = signer.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = w3.eth.send_raw_transaction(raw)
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=s.chain_tx_timeout_seconds)
        except ChainUnavailable:
            raise
        except Exception as error:
            raise ChainUnavailable(f"{label} failed: {type(error).__name__}: {error}") from error

        result = ChainReceipt(
            tx_hash=receipt["transactionHash"].hex(),
            block_number=receipt["blockNumber"],
            gas_used=receipt["gasUsed"],
            status=receipt["status"],
            explorer=f"https://sepolia.etherscan.io/tx/{receipt['transactionHash'].hex()}",
        )
        if not result.succeeded:
            raise ChainUnavailable(f"{label} reverted on chain ({result.tx_hash})")
        logger.info("%s confirmed in block %s, %s gas (%s)",
                    label, result.block_number, result.gas_used, result.tx_hash)
        return result


# ------------------------------------------------------------- helpers --

def _to_bytes32(w3, value: str) -> bytes:
    """A 0x-prefixed or bare hex digest as the 32 bytes the ABI expects."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    text = value[2:] if value.startswith("0x") else value
    return bytes.fromhex(text)


def _to_bytes32_text(text: str) -> bytes:
    """A short string right-padded to 32 bytes, as `encodeBytes32String` does."""
    encoded = text.encode()
    if len(encoded) > 32:
        raise ValueError(f"{text!r} does not fit in bytes32")
    return encoded + bytes(32 - len(encoded))


def _plain(value: Any) -> Any:
    """Event argument values in forms json.dumps accepts."""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return value


def _decode_escrow(raw: Any) -> dict[str, Any]:
    """getEscrow's tuple, named. Field order follows EscrowContract.Escrow."""
    fields = (
        "buyer", "seller", "arbitrator", "amount", "state", "outcome",
        "riskScore", "createdAt", "deliveryConfirmedAt", "timeLockEndsAt",
        "holdingPeriodEndsAt", "exists",
    )
    out: dict[str, Any] = {}
    for index, name in enumerate(fields):
        if index >= len(raw):
            break
        out[name] = _plain(raw[index])
    if isinstance(out.get("state"), int) and out["state"] < len(CHAIN_STATES):
        out["stateName"] = CHAIN_STATES[out["state"]]
    if isinstance(out.get("outcome"), int) and out["outcome"] < len(CHAIN_OUTCOMES):
        out["outcomeName"] = CHAIN_OUTCOMES[out["outcome"]]
    return out


# ------------------------------------------------------------ singleton --

_client: ChainClient | None = None
_client_lock = threading.Lock()


def get_chain(settings: Settings) -> ChainClient:
    """One client per process, built on first use.

    Built lazily so the service starts, and its tests run, on a machine with no
    RPC endpoint and no keys.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = ChainClient(settings)
    return _client


def reset_chain() -> None:
    """Drops the cached client. For tests, and after a settings change."""
    global _client
    with _client_lock:
        _client = None
