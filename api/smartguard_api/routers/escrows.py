"""Escrow lifecycle — FR-01, FR-03, FR-04, FR-05, FR-06, FR-07.

Every route enforces the same object-level guards the contract enforces at the
EVM level. The duplication is intentional: the contract is the boundary that
cannot be bypassed, and these checks exist so the service never attempts a call
the chain would revert, and never returns a record the caller is not entitled
to see.

Note what the read routes do *not* do. `GET /escrows/{id}` returns 404, not 403,
for an escrow the caller may not see. A 403 would confirm that an escrow with
that identifier exists, which is information the caller has no right to.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..features import build_vector
from ..inference import InferenceUnavailable
from ..models import (
    AuditLayer, Dispute, EscrowState, FraudPrediction, Outcome, Role, Transaction, User,
)
from ..schemas import (
    CreateEscrowRequest, DisputeOut, EscrowOut, RaiseDisputeRequest, ResolveDisputeRequest,
)
from ..security import CurrentUser, require
from ..settings_store import current as current_settings
from .. import audit, chain_tasks, policy
from ..chain import get_chain
from ..models import ChainStatus

router = APIRouter(prefix="/escrows", tags=["escrows"])

NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="No escrow with that reference is visible to your account.",
)


# ------------------------------------------------------------- helpers -----

def _visible(user: User, db: Session):
    """The query scoped to what this principal may read.

    Applied server-side, so a client asking for another party's escrow receives
    nothing regardless of what it renders.
    """
    query = db.query(Transaction)
    if policy.can(user, "escrow:read:any"):
        return query
    return query.filter(
        or_(
            Transaction.buyer_id == user.id,
            Transaction.seller_id == user.id,
            Transaction.arbitrator_id == user.id,
        ),
    )


def _load(reference: str, user: User, db: Session) -> Transaction:
    tx = db.query(Transaction).filter(Transaction.reference == reference).one_or_none()
    if tx is None or not policy.can_read_escrow(user, tx):
        raise NOT_FOUND
    return tx


def _permitted_actions(user: User, tx: Transaction, config: dict) -> list[str]:
    """What this principal may do next, so the interface need not reimplement
    the policy to decide which controls to render."""
    actions = []
    if policy.can_confirm_delivery(user, tx):
        actions.append("confirm-delivery")
    if policy.can_claim_refund(user, tx):
        actions.append("claim-refund")
    if policy.can_withdraw(user, tx, int(config["timeLockHours"])):
        actions.append("withdraw")
    if policy.can_raise_dispute(user, tx):
        actions.append("raise-dispute")

    dispute = tx.dispute
    if dispute is not None and policy.can_resolve_dispute(user, tx, dispute):
        actions.append("resolve-dispute")
    return actions


def _serialise(tx: Transaction, user: User, config: dict) -> EscrowOut:
    out = EscrowOut.model_validate(tx)
    out.permittedActions = _permitted_actions(user, tx, config)
    return out


# How many times to retry a reference collision before giving up. Each attempt
# recomputes from the current row count, so a handful covers far more
# concurrency than NFR-05's thirty cycles per minute.
_REFERENCE_ATTEMPTS = 8


def _next_reference(db: Session) -> str:
    """The next human-readable escrow reference.

    Derived from the row count, which is a read-modify-write and therefore
    racy: two concurrent requests both see the same count, both build
    `ESC-1042`, and the second violates the unique index. Under load testing
    this appeared as a 500 on roughly four per cent of escrow creations. A
    single-threaded test suite cannot reach it.

    The count is kept rather than replaced with a sequence because the
    reference is user-visible and sequential by design, and because a
    PostgreSQL sequence would behave differently under SQLite, where the
    suites run. `_insert_with_reference` makes the write safe instead, by
    retrying on the collision this function cannot prevent.
    """
    count = db.query(Transaction).count()
    return f"ESC-{1042 + count}"


def _insert_with_reference(db: Session, build: Callable[[str], Transaction]) -> Transaction:
    """Inserts a transaction, retrying if its reference was taken concurrently.

    `build` is called with a candidate reference and returns the unfinished
    row. It is called again on each attempt, because a rolled-back ORM object
    cannot be reused.

    Only an integrity error on the reference is retried. Any other failure is
    re-raised, so a genuine fault is not retried eight times and then reported
    as a collision.
    """
    for attempt in range(_REFERENCE_ATTEMPTS):
        reference = _next_reference(db)
        savepoint = db.begin_nested()
        try:
            tx = build(reference)
            db.add(tx)
            db.flush()
        except IntegrityError as error:
            savepoint.rollback()
            if "reference" not in str(error.orig).lower():
                raise
            if attempt == _REFERENCE_ATTEMPTS - 1:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "Could not allocate an escrow reference under the "
                        "current load. Please retry."
                    ),
                ) from error
            continue
        else:
            savepoint.commit()
            return tx

    raise AssertionError("unreachable")


# ------------------------------------------------------------- FR-01/02 ----

@router.post(
    "",
    response_model=EscrowOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require("escrow:create"))],
)
def create_escrow(
    payload: CreateEscrowRequest,
    background: BackgroundTasks,
    request: Request,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    """Scores the request and, if it clears the gate, records the escrow.

    The score is recomputed here rather than accepted from the client. A client
    that could supply its own risk score would make the gate decorative.
    """
    config = current_settings(db, settings)
    threshold = float(config["riskThreshold"])

    seller = db.get(User, payload.sellerId)
    if seller is None or seller.role != Role.SELLER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That seller does not exist.",
        )
    if seller.id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An escrow needs two distinct parties.",
        )

    arbitrator = db.query(User).filter(User.role == Role.ARBITRATOR).first()
    if arbitrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No arbitrator is available to adjudicate this escrow.",
        )

    scorer = request.app.state.scorer
    try:
        assessment = scorer.score(build_vector(payload.scoring))
    except InferenceUnavailable as error:
        audit.record(
            db, layer=AuditLayer.APPLICATION, event_type="ai.inference.failed",
            summary=f"Risk scoring failed during escrow initiation: {error}",
            actor=user,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The risk-scoring service is unavailable, so this escrow cannot "
                "be initiated. No funds have been committed."
            ),
        ) from error

    # FR-02. Refused before anything is written, and before any on-chain call.
    if assessment.risk_score > threshold:
        audit.record(
            db, layer=AuditLayer.APPLICATION, event_type="risk.gate.reject",
            summary=(
                f"Initiation refused: score {assessment.risk_score:.4f} exceeds the "
                f"configured gate of {threshold:.2f}. No gas consumed, no funds "
                "committed."
            ),
            actor=user,
            detail={"riskScore": assessment.risk_score, "threshold": threshold},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Risk assessment rejected this escrow: {assessment.risk_score:.2f} "
                f"exceeds the configured gate of {threshold:.2f}."
            ),
        )

    now = datetime.now(timezone.utc)
    nonce = secrets.randbits(48)

    # Is the chain layer going to pick this up? The real identifier and nonce
    # come back from the oracle write, so until that lands the row carries a
    # placeholder and says so, rather than showing an identifier no contract
    # would recognise.
    chain = get_chain(settings)
    chain_status = (
        ChainStatus.PENDING if chain.available else ChainStatus.DISABLED
    )

    tx = _insert_with_reference(db, lambda reference: Transaction(
        # Replaced by the keccak256 identifier the contract derives, once the
        # oracle write confirms. Until then it is marked as provisional.
        escrow_id=f"pending:{nonce}",
        chain_status=chain_status,
        reference=reference,
        buyer_id=user.id,
        seller_id=seller.id,
        arbitrator_id=arbitrator.id,
        amount_eth=payload.amountEth,
        item_description=payload.itemDescription,
        state=EscrowState.INITIATED,
        nonce=nonce,
        created_at=now,
        holding_period_ends_at=now + timedelta(hours=int(config["holdingPeriodHours"])),
    ))

    db.add(FraudPrediction(
        transaction_id=tx.id,
        risk_score=assessment.risk_score,
        model_version=assessment.model_version,
        threshold_at_scoring=threshold,
        latency_ms=assessment.latency_ms,
        exceeded_budget=assessment.exceeded_budget,
        feature_vector=build_vector(payload.scoring),
        contributions=assessment.contributions,
    ))

    audit.record(
        db, layer=AuditLayer.PRESENTATION, event_type="ui.escrow.submit",
        summary=(
            f"Buyer submitted an escrow request for {payload.amountEth} ETH to "
            f"{seller.display_name}."
        ),
        transaction_id=tx.id, actor=user,
    )
    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="ai.inference",
        summary=(
            f"Risk score {assessment.risk_score:.4f} returned in "
            f"{assessment.latency_ms} ms (model {assessment.model_version})."
        ),
        transaction_id=tx.id, actor=user,
    )
    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="risk.gate.pass",
        summary=(
            f"Risk gate passed: {assessment.risk_score:.4f} ≤ {threshold:.2f}. "
            + ("Awaiting oracle write and on-chain deposit."
               if chain_status is ChainStatus.PENDING
               else "The chain layer is switched off; no on-chain record follows.")
        ),
        transaction_id=tx.id, actor=user,
    )

    db.commit()
    db.refresh(tx)

    # Off the request path on purpose: the oracle write and the deposit are two
    # Sepolia transactions, about eleven seconds each, and a twenty-second
    # response is treated as a fault by browsers and proxies alike. The row is
    # returned as PENDING and the interface watches it settle.
    if chain_status is ChainStatus.PENDING:
        background.add_task(chain_tasks.submit_escrow, tx.id, assessment.risk_score)

    return _serialise(tx, user, config)


# ---------------------------------------------------------------- FR-07 ----

@router.get("", response_model=list[EscrowOut])
def list_escrows(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    state: EscrowState | None = None,
) -> list[EscrowOut]:
    config = current_settings(db, settings)
    query = _visible(user, db)
    if state is not None:
        query = query.filter(Transaction.state == state)
    rows = query.order_by(Transaction.created_at.desc()).all()
    return [_serialise(tx, user, config) for tx in rows]


@router.get("/{reference}", response_model=EscrowOut)
def get_escrow(
    reference: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    config = current_settings(db, settings)
    return _serialise(_load(reference, user, db), user, config)


# ---------------------------------------------------------------- FR-03 ----

@router.post("/{reference}/confirm-delivery", response_model=EscrowOut)
def confirm_delivery(
    reference: str,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    config = current_settings(db, settings)
    tx = _load(reference, user, db)

    if not policy.can_confirm_delivery(user, tx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the buyer may confirm delivery, and only while the escrow is INITIATED.",
        )

    tx.state = EscrowState.DELIVERED
    tx.delivery_confirmed_at = datetime.now(timezone.utc)

    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="escrow.confirm-delivery",
        summary=(
            f"Buyer confirmed receipt. {config['timeLockHours']}-hour time lock "
            "started."
        ),
        transaction_id=tx.id, actor=user,
    )
    db.commit()
    db.refresh(tx)
    # Queued, not awaited: the contract call takes about eleven seconds on
    # Sepolia. The database has already recorded the transition, and the
    # event listener reconciles the two with the chain as the authority.
    background.add_task(chain_tasks.submit_action, tx.id, "confirmDelivery")
    return _serialise(tx, user, config)


@router.post("/{reference}/withdraw", response_model=EscrowOut)
def withdraw(
    reference: str,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    config = current_settings(db, settings)
    tx = _load(reference, user, db)

    if not policy.can_withdraw(user, tx, int(config["timeLockHours"])):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Funds are released to the seller only after the buyer has "
                "confirmed delivery and the time lock has elapsed without a dispute."
            ),
        )

    tx.state = EscrowState.COMPLETED
    tx.outcome = Outcome.RELEASED_TO_SELLER
    tx.completed_at = datetime.now(timezone.utc)

    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="escrow.release",
        summary=(
            f"Time lock elapsed without dispute. {tx.amount_eth} ETH released to "
            f"{tx.seller.display_name}. State → COMPLETED."
        ),
        transaction_id=tx.id, actor=user,
    )
    db.commit()
    db.refresh(tx)
    # Queued, not awaited: the contract call takes about eleven seconds on
    # Sepolia. The database has already recorded the transition, and the
    # event listener reconciles the two with the chain as the authority.
    background.add_task(chain_tasks.submit_action, tx.id, "releaseFunds")
    return _serialise(tx, user, config)


# ---------------------------------------------------------------- FR-04 ----

@router.post("/{reference}/dispute", response_model=DisputeOut, status_code=201)
def raise_dispute(
    reference: str,
    background: BackgroundTasks,
    payload: RaiseDisputeRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Dispute:
    tx = _load(reference, user, db)

    if not policy.can_raise_dispute(user, tx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the buyer or the seller may raise a dispute, and only while "
                "the escrow is INITIATED or DELIVERED."
            ),
        )

    raised_by = policy.dispute_role_of(user, tx)
    count = db.query(Dispute).count()

    dispute = Dispute(
        reference=f"DSP-{205 + count}",
        transaction_id=tx.id,
        arbitrator_id=tx.arbitrator_id,
        raised_by_role=raised_by,
        reason=payload.reason,
    )
    db.add(dispute)
    tx.state = EscrowState.DISPUTED

    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="escrow.dispute",
        summary=(
            f"Dispute raised by {raised_by.value}. Decision authority transferred "
            "to the arbitrator."
        ),
        transaction_id=tx.id, actor=user,
    )
    db.commit()
    db.refresh(dispute)
    # Queued, not awaited: the contract call takes about eleven seconds on
    # Sepolia. The database has already recorded the transition, and the
    # event listener reconciles the two with the chain as the authority.
    background.add_task(chain_tasks.submit_action, tx.id, "initiateDispute")
    return dispute


# ---------------------------------------------------------------- FR-05 ----

@router.post("/{reference}/resolve", response_model=EscrowOut)
def resolve_dispute(
    reference: str,
    background: BackgroundTasks,
    payload: ResolveDisputeRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    config = current_settings(db, settings)
    tx = _load(reference, user, db)
    dispute = tx.dispute

    if dispute is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This escrow has no open dispute.",
        )

    if not policy.can_resolve_dispute(user, tx, dispute):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only the arbitrator named in this escrow may resolve it, and "
                "only once."
            ),
        )

    now = datetime.now(timezone.utc)
    dispute.resolution_for_seller = payload.releaseToSeller
    dispute.resolution_note = payload.note
    dispute.resolved_at = now

    tx.state = EscrowState.COMPLETED
    tx.outcome = (
        Outcome.RESOLVED_FOR_SELLER if payload.releaseToSeller
        else Outcome.RESOLVED_FOR_BUYER
    )
    tx.completed_at = now

    direction = "seller" if payload.releaseToSeller else "buyer"
    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="escrow.resolve",
        summary=(
            f"Arbitrator resolved in favour of the {direction}. {tx.amount_eth} ETH "
            f"{'released' if payload.releaseToSeller else 'refunded'}. "
            "State → COMPLETED."
        ),
        transaction_id=tx.id, actor=user,
    )
    db.commit()
    db.refresh(tx)
    # Queued, not awaited: the contract call takes about eleven seconds on
    # Sepolia. The database has already recorded the transition, and the
    # event listener reconciles the two with the chain as the authority.
    background.add_task(chain_tasks.submit_action, tx.id, "resolveDispute", release_to_seller=payload.releaseToSeller)
    return _serialise(tx, user, config)


# ---------------------------------------------------------------- FR-06 ----

@router.post("/{reference}/refund", response_model=EscrowOut)
def claim_refund(
    reference: str,
    background: BackgroundTasks,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EscrowOut:
    config = current_settings(db, settings)
    tx = _load(reference, user, db)

    if not policy.can_claim_refund(user, tx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "A refund may be claimed only by the buyer, only while the escrow "
                "is INITIATED, and only after the holding period has elapsed."
            ),
        )

    tx.state = EscrowState.COMPLETED
    tx.outcome = Outcome.TIMEOUT_REFUND
    tx.completed_at = datetime.now(timezone.utc)

    audit.record(
        db, layer=AuditLayer.APPLICATION, event_type="escrow.refund",
        summary=(
            "Holding period elapsed with no delivery confirmation. "
            f"{tx.amount_eth} ETH refunded to buyer. State → COMPLETED."
        ),
        transaction_id=tx.id, actor=user,
    )
    db.commit()
    db.refresh(tx)
    # Queued, not awaited: the contract call takes about eleven seconds on
    # Sepolia. The database has already recorded the transition, and the
    # event listener reconciles the two with the chain as the authority.
    background.add_task(chain_tasks.submit_action, tx.id, "claimRefund")
    return _serialise(tx, user, config)
