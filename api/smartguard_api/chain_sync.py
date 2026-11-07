"""Projecting the chain onto the database — the listener Section 3.14 assumes.

`Transaction` says of itself that its state is "kept in step with the chain by
the event listener, never written speculatively by a request handler". No such
listener existed, so the sentence described an intention. This is it.

The rule it enforces is that **the contract is the authority**. A request
handler writes the transition it believes it made, because it has to answer
the request; this reconciles that belief against what the chain actually
recorded. Where they disagree the chain wins, and the disagreement is written
to the audit log rather than quietly overwritten — a database that silently
conforms to the chain hides exactly the bug worth finding.

It reconciles **by state, not by log scan**, and that is a deliberate
correction rather than a shortcut. The first version walked `eth_getLogs`
forward from the deployment block. Alchemy's free tier caps that call at a ten
block range, so every request came back `-32600` and the listener advanced its
cursor having read nothing — the worst kind of failure, because it looked
healthy. Catching up sixty-eight thousand blocks at ten per poll would have
taken over a day in any case.

`getEscrow` is an `eth_call` with no range limit, and the escrows worth
reconciling are the ones this database already knows the identifiers of. So the
listener asks the contract about its own open escrows, which costs one call per
open escrow and is bounded by the workload rather than by the length of the
chain.

Logs are still read, for the detail a state read cannot give — which block, and
which transaction hash — but only across the last few blocks, inside whatever
range the provider allows. The cursor in `SystemSetting` bounds that window and
is clamped near the head, never crawled from the deployment block.

It polls rather than subscribes, because the HTTP providers this project uses
do not offer websockets on their free tier.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .chain import DEPLOYMENT_RECORD, ChainClient, ChainUnavailable, get_chain
from .config import Settings, get_settings
from .db import SessionLocal
from .models import (
    AuditLayer, ChainStatus, EscrowState, Outcome, SystemSetting, Transaction,
)

logger = logging.getLogger(__name__)

CURSOR_KEY = "chainSyncBlock"

# Alchemy's free tier rejects an `eth_getLogs` range wider than ten blocks with
# `-32600`. Kept at the limit rather than above it: a request that is refused
# reads no events at all, and a listener that advances its cursor regardless
# looks like it is working.
MAX_RANGE = 10

# How far back the log scan may reach from the head. Logs supply the block and
# transaction hash for the audit record; the authoritative state comes from
# `getEscrow`, so falling behind on logs loses detail, not correctness.
MAX_LOOKBACK = 200

# DisputeResolved and FundsReleased both end an escrow; these map the chain's
# uint8 outcome onto the enum the database stores.
OUTCOME_BY_ORDINAL = {
    1: Outcome.RELEASED_TO_SELLER,
    2: Outcome.RESOLVED_FOR_SELLER,
    3: Outcome.RESOLVED_FOR_BUYER,
    4: Outcome.TIMEOUT_REFUND,
}

STATE_BY_EVENT = {
    "FundsDeposited": EscrowState.INITIATED,
    "DeliveryConfirmed": EscrowState.DELIVERED,
    "DisputeInitiated": EscrowState.DISPUTED,
    "DisputeResolved": EscrowState.COMPLETED,
    "FundsReleased": EscrowState.COMPLETED,
}


def _normalise(escrow_id: Any) -> str:
    """Event ids arrive as bytes or hex; the column stores 0x-prefixed hex."""
    if isinstance(escrow_id, (bytes, bytearray)):
        return "0x" + bytes(escrow_id).hex()
    text = str(escrow_id)
    return text if text.startswith("0x") else f"0x{text}"


def _deployment_block() -> int:
    """The earliest block that could carry one of our events."""
    if not DEPLOYMENT_RECORD.exists():
        return 0
    record = json.loads(DEPLOYMENT_RECORD.read_text())
    blocks = [
        int(c["blockNumber"]) for c in record.get("contracts", {}).values()
        if c.get("blockNumber")
    ]
    return min(blocks) if blocks else 0


def read_cursor(db: Session) -> int | None:
    row = db.get(SystemSetting, CURSOR_KEY)
    if row is None or not str(row.value).strip():
        return None
    try:
        return int(row.value)
    except ValueError:
        return None


def write_cursor(db: Session, block: int) -> None:
    row = db.get(SystemSetting, CURSOR_KEY)
    if row is None:
        db.add(SystemSetting(key=CURSOR_KEY, value=str(block)))
    else:
        row.value = str(block)


def _apply(db: Session, event: dict) -> bool:
    """Applies one event to its transaction. Returns True when something changed."""
    args = event["args"]
    raw_id = args.get("escrowId")
    if raw_id is None:
        return False
    escrow_id = _normalise(raw_id)

    tx = db.execute(
        select(Transaction).where(Transaction.escrow_id == escrow_id)
    ).scalars().first()
    if tx is None:
        # An escrow this database did not create — the measurement scripts put
        # several on the same contracts. Not an error, and not ours to record.
        return False

    name = event["event"]
    changed = False

    # A deposit confirms the record regardless of what the submitting task
    # managed to write back, which is what makes this a reconciliation rather
    # than a duplicate of chain_tasks.
    if name == "FundsDeposited" and tx.chain_status is not ChainStatus.CONFIRMED:
        tx.chain_status = ChainStatus.CONFIRMED
        tx.chain_block = event["block_number"]
        tx.deposit_tx_hash = tx.deposit_tx_hash or event["tx_hash"]
        changed = True

    target = STATE_BY_EVENT.get(name)
    if target is not None and tx.state is not target:
        previous = tx.state
        tx.state = target
        changed = True
        audit.record(
            db, layer=AuditLayer.BLOCKCHAIN, event_type="chain.state.reconciled",
            summary=(
                f"{tx.reference}: the database held {previous.value} and the "
                f"contract reports {target.value} after {name}. The chain is "
                "the authority, so the record was corrected."
            ),
            transaction_id=tx.id,
            detail={
                "event": name,
                "txHash": event["tx_hash"],
                "blockNumber": event["block_number"],
                "previousState": previous.value,
                "state": target.value,
            },
        )

    if name == "DisputeResolved":
        outcome = (Outcome.RESOLVED_FOR_SELLER if args.get("releasedToSeller")
                   else Outcome.RESOLVED_FOR_BUYER)
        if tx.outcome is not outcome:
            tx.outcome = outcome
            changed = True
        tx.settlement_tx_hash = tx.settlement_tx_hash or event["tx_hash"]

    if name == "FundsReleased":
        ordinal = args.get("outcome")
        outcome = OUTCOME_BY_ORDINAL.get(int(ordinal)) if ordinal is not None else None
        if outcome is not None and tx.outcome is not outcome:
            tx.outcome = outcome
            changed = True
        tx.settlement_tx_hash = tx.settlement_tx_hash or event["tx_hash"]

    return changed


STATE_BY_NAME = {
    "INITIATED": EscrowState.INITIATED,
    "DELIVERED": EscrowState.DELIVERED,
    "DISPUTED": EscrowState.DISPUTED,
    "COMPLETED": EscrowState.COMPLETED,
}


def reconcile_open_escrows(db: Session, chain: ChainClient) -> dict[str, int]:
    """Asks the contract about every escrow this database still considers open.

    One `eth_call` per open escrow, which is bounded by how much business is in
    flight rather than by how long the chain is. Escrows that are COMPLETED in
    both places are left alone; there is nothing left to learn about them.
    """
    open_rows = db.execute(
        select(Transaction).where(
            Transaction.chain_status == ChainStatus.CONFIRMED,
            Transaction.state != EscrowState.COMPLETED,
        )
    ).scalars().all()

    checked = corrected = 0
    for tx in open_rows:
        if not tx.escrow_id.startswith("0x"):
            continue
        checked += 1
        try:
            on_chain = chain.get_escrow(tx.escrow_id)
        except ChainUnavailable as error:
            logger.warning("could not read %s from the contract: %s", tx.reference, error)
            continue
        if not on_chain:
            continue

        target = STATE_BY_NAME.get(on_chain.get("stateName", ""))
        if target is None or tx.state is target:
            continue

        previous = tx.state
        tx.state = target
        corrected += 1

        outcome_name = on_chain.get("outcomeName")
        if outcome_name and outcome_name != "NONE":
            try:
                tx.outcome = Outcome[outcome_name]
            except KeyError:
                logger.warning("unmapped on-chain outcome %s", outcome_name)

        audit.record(
            db, layer=AuditLayer.BLOCKCHAIN, event_type="chain.state.reconciled",
            summary=(
                f"{tx.reference}: the database held {previous.value} and the "
                f"contract reports {target.value}. The chain is the authority, "
                "so the record was corrected."
            ),
            transaction_id=tx.id,
            detail={
                "source": "getEscrow",
                "previousState": previous.value,
                "state": target.value,
                "onChainOutcome": outcome_name,
            },
        )

    return {"checked": checked, "corrected": corrected}


def sync_once(settings: Settings | None = None,
              chain: ChainClient | None = None) -> dict[str, Any]:
    """Reads one window of events and applies them. Safe to call repeatedly."""
    settings = settings or get_settings()
    chain = chain or get_chain(settings)

    if not chain.available:
        return {"ran": False, "reason": "the chain layer is unavailable"}

    db = SessionLocal()
    try:
        head = chain.block_number()

        # --- 1. the authoritative pass ---------------------------------
        state = reconcile_open_escrows(db, chain)

        # --- 2. the detail pass, inside the provider's range limit -----
        # Clamped near the head rather than crawled from the deployment
        # block: at ten blocks a poll, catching up from the deployment would
        # take longer than the demonstration it is meant to support, and the
        # state pass above has already established what is true.
        cursor = read_cursor(db)
        floor = max(head - MAX_LOOKBACK, 0)
        if cursor is None or cursor < floor:
            cursor = floor

        events: list[dict] = []
        applied = 0
        to_block = cursor
        if cursor < head:
            to_block = min(head, cursor + MAX_RANGE)
            events = chain.fetch_events(cursor + 1, to_block)
            applied = sum(1 for event in events if _apply(db, event))

        write_cursor(db, to_block)
        db.commit()

        if applied or state["corrected"]:
            logger.info(
                "chain sync: %s/%s escrows corrected by state, %s of %s events "
                "applied up to block %s",
                state["corrected"], state["checked"], applied, len(events), to_block,
            )
        return {
            "ran": True, "head": head,
            "checked": state["checked"], "corrected": state["corrected"],
            "from": cursor + 1, "to": to_block,
            "events": len(events), "applied": applied,
        }
    except ChainUnavailable as error:
        db.rollback()
        return {"ran": False, "reason": str(error)}
    finally:
        db.close()


# ------------------------------------------------------------ listener ---

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop(settings: Settings) -> None:
    logger.info("chain listener started, polling every %ss", settings.chain_poll_seconds)
    while not _stop.is_set():
        try:
            sync_once(settings)
        except Exception as error:          # pragma: no cover - defensive
            # A listener that dies on one bad window stops reconciling
            # everything, which is worse than logging and trying again.
            logger.exception("chain sync raised: %s", error)
        _stop.wait(settings.chain_poll_seconds)
    logger.info("chain listener stopped")


def start_listener(settings: Settings) -> bool:
    """Starts the poller once. Returns whether it is now running."""
    global _thread
    if not settings.chain_sync_enabled:
        return False
    if not get_chain(settings).available:
        logger.warning("chain sync requested but the chain layer is unavailable")
        return False
    if _thread is not None and _thread.is_alive():
        return True

    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(settings,),
                               name="chain-listener", daemon=True)
    _thread.start()
    return True


def stop_listener() -> None:
    _stop.set()
