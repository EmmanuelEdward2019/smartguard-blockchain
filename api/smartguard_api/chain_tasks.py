"""Chain work that must not happen on the request path.

A Sepolia transaction takes about eleven seconds to be included, and creating
an escrow needs two of them — the oracle write and the deposit. Doing that
inside the HTTP handler would make escrow creation a twenty-second request,
which browsers and proxies both treat as a fault.

So the handler writes the row, commits, and returns. These functions then run
afterwards and fill in what the chain says. Until they finish the row carries
`chain_status = PENDING`, which the interface shows as such rather than
presenting an unconfirmed escrow as settled.

Each function opens its own session. The request's session is closed by the
time a background task runs, and reusing it would use a connection that has
already been returned to the pool.

**Which addresses go on chain.** The seeded accounts hold demonstration wallet
addresses whose keys nobody has, so they cannot sign. The chain therefore sees
the service's own accounts in the roles that must act — the funder as buyer,
the oracle operator as arbitrator — and the seller's recorded address as the
seller, which never needs to sign in these paths. Every such substitution is
written to the audit log, so the on-chain record can be read against the
off-chain one without guessing. Section 3.15 should state it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from . import audit
from .chain import ChainReceipt, ChainUnavailable, get_chain
from .config import Settings, get_settings
from .db import SessionLocal
from .models import AuditLayer, ChainStatus, EscrowState, Outcome, Transaction

logger = logging.getLogger(__name__)


def _session() -> Session:
    return SessionLocal()


def _load(db: Session, transaction_id: str) -> Transaction | None:
    tx = db.get(Transaction, transaction_id)
    if tx is None:
        logger.warning("chain task for unknown transaction %s", transaction_id)
    return tx


def _record_failure(db: Session, tx: Transaction, error: Exception, step: str) -> None:
    """Marks the row failed and says why, rather than leaving it pending.

    A row stuck on PENDING is indistinguishable from one still in flight, so a
    failure that is not recorded reads as a transaction that never finished.
    """
    tx.chain_status = ChainStatus.FAILED
    tx.chain_error = f"{step}: {error}"
    audit.record(
        db, layer=AuditLayer.ORACLE, event_type="chain.submit.failed",
        summary=f"{step} failed for {tx.reference}: {error}",
        transaction_id=tx.id,
    )
    db.commit()
    logger.error("chain %s failed for %s: %s", step, tx.reference, error)


def _receipt_detail(receipt: ChainReceipt) -> dict[str, Any]:
    """The explorer link, for the detail payload.

    The hash, gas and block go into AuditLog's own columns rather than here —
    see `_receipt_columns`. They were previously written only into this JSON,
    which left the interface summing a `gas_used` column that was always null
    and reporting "0 gas across 0 calls" on escrows that had consumed plenty.
    """
    return {"explorer": receipt.explorer}


def _receipt_columns(receipt: ChainReceipt) -> dict[str, Any]:
    """The receipt fields audit.record stores as columns, so they are queryable."""
    return {
        "tx_hash": receipt.tx_hash,
        "gas_used": receipt.gas_used,
        "block_number": receipt.block_number,
    }


# --------------------------------------------------------------- create ---

def submit_escrow(transaction_id: str, risk_score: float,
                  settings: Settings | None = None) -> None:
    """Writes the score on chain, then funds the escrow.

    Order is not negotiable: `depositFunds` asks the oracle for a score and
    reverts when there is none, so the write has to land first. That is the
    fail-closed behaviour Section 3.15.2 specifies, observed here rather than
    asserted.
    """
    settings = settings or get_settings()
    chain = get_chain(settings)
    db = _session()
    try:
        tx = _load(db, transaction_id)
        if tx is None:
            return

        if not chain.available:
            tx.chain_status = ChainStatus.DISABLED
            db.commit()
            return

        w3 = chain._w3
        buyer_address = chain._funder.address
        arbitrator_address = chain._operator.address
        seller_address = tx.seller.wallet_address
        amount_wei = w3.to_wei(settings.chain_demo_amount_eth, "ether")

        if seller_address.lower() in {buyer_address.lower(), arbitrator_address.lower()}:
            _record_failure(
                db, tx,
                ValueError(
                    "the seller's address matches the funder or the operator; "
                    "depositFunds requires three distinct parties"),
                "party check")
            return

        # --- 1. the oracle write ---------------------------------------
        try:
            escrow_id, nonce, score_receipt = chain.write_risk_score(
                buyer_address, seller_address, amount_wei, risk_score)
        except ChainUnavailable as error:
            _record_failure(db, tx, error, "updateRiskScore")
            return

        tx.escrow_id = f"0x{escrow_id}" if not escrow_id.startswith("0x") else escrow_id
        tx.nonce = nonce
        tx.score_tx_hash = score_receipt.tx_hash
        tx.chain_amount_wei = amount_wei
        audit.record(
            db, layer=AuditLayer.ORACLE, event_type="contract.updateRiskScore",
            summary=(
                f"Oracle wrote risk score {round(risk_score * 100)} to "
                f"RiskOracleContract for {tx.reference} (nonce {nonce})."
            ),
            transaction_id=tx.id, detail=_receipt_detail(score_receipt),
            **_receipt_columns(score_receipt),
        )
        db.commit()

        # --- 2. the deposit --------------------------------------------
        try:
            deposit_receipt = chain.deposit(
                seller_address, arbitrator_address, nonce, amount_wei)
        except ChainUnavailable as error:
            _record_failure(db, tx, error, "depositFunds")
            return

        tx.deposit_tx_hash = deposit_receipt.tx_hash
        tx.chain_block = deposit_receipt.block_number
        tx.chain_status = ChainStatus.CONFIRMED
        tx.chain_error = None
        audit.record(
            db, layer=AuditLayer.BLOCKCHAIN, event_type="contract.depositFunds",
            summary=(
                f"{settings.chain_demo_amount_eth} ETH locked in EscrowContract "
                f"for {tx.reference}. Risk gate passed. State → INITIATED."
            ),
            transaction_id=tx.id,
            **_receipt_columns(deposit_receipt),
            detail={
                **_receipt_detail(deposit_receipt),
                "escrowId": tx.escrow_id,
                "onChainBuyer": buyer_address,
                "onChainSeller": seller_address,
                "onChainArbitrator": arbitrator_address,
                "notionalAmountEth": tx.amount_eth,
                "depositedAmountEth": settings.chain_demo_amount_eth,
            },
        )
        db.commit()
        logger.info("escrow %s confirmed on chain as %s", tx.reference, tx.escrow_id)
    finally:
        db.close()


# -------------------------------------------------------------- actions ---

def submit_action(transaction_id: str, action: str,
                  release_to_seller: bool | None = None,
                  settings: Settings | None = None) -> None:
    """Mirrors a lifecycle action onto the contract.

    The database has already recorded the transition by the time this runs —
    the handler needs to answer the request — so a chain failure leaves the two
    disagreeing. That is recorded on the row and reconciled by the event
    listener, which treats the chain as the authority.
    """
    settings = settings or get_settings()
    chain = get_chain(settings)
    db = _session()
    try:
        tx = _load(db, transaction_id)
        if tx is None:
            return
        if not chain.available:
            return
        if tx.chain_status is not ChainStatus.CONFIRMED or not tx.escrow_id.startswith("0x"):
            logger.info(
                "skipping on-chain %s for %s: it has no confirmed escrow",
                action, tx.reference)
            return

        calls: dict[str, Callable[[], ChainReceipt]] = {
            "confirmDelivery": lambda: chain.confirm_delivery(tx.escrow_id),
            "initiateDispute": lambda: chain.initiate_dispute(tx.escrow_id),
            "resolveDispute": lambda: chain.resolve_dispute(
                tx.escrow_id, bool(release_to_seller)),
            "releaseFunds": lambda: chain.release_funds(tx.escrow_id),
            "claimRefund": lambda: chain.claim_refund(tx.escrow_id),
        }
        call = calls.get(action)
        if call is None:
            logger.warning("no chain call named %s", action)
            return

        try:
            receipt = call()
        except ChainUnavailable as error:
            tx.chain_error = f"{action}: {error}"
            audit.record(
                db, layer=AuditLayer.BLOCKCHAIN, event_type="chain.action.failed",
                summary=f"{action} failed on chain for {tx.reference}: {error}",
                transaction_id=tx.id,
            )
            db.commit()
            logger.error("chain %s failed for %s: %s", action, tx.reference, error)
            return

        if action in {"resolveDispute", "releaseFunds", "claimRefund"}:
            tx.settlement_tx_hash = receipt.tx_hash
        tx.chain_error = None
        audit.record(
            db, layer=AuditLayer.BLOCKCHAIN, event_type=f"contract.{action}",
            summary=f"{action} confirmed on chain for {tx.reference}.",
            transaction_id=tx.id, detail=_receipt_detail(receipt),
            **_receipt_columns(receipt),
        )
        db.commit()
        logger.info("chain %s confirmed for %s (%s)", action, tx.reference, receipt.tx_hash)
    finally:
        db.close()
