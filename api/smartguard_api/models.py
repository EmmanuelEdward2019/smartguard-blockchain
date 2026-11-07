"""Off-chain schema — Section 3.9.2.

Five entities: User, Transaction, FraudPrediction, Dispute, AuditLog.

The governing principle from Section 3.9 is that only what is needed for
tamper-proof enforcement goes on chain; everything else lives here. Two
consequences shape the schema:

  - Personally identifiable information is confined to `users`, so it can be
    modified or erased under data-protection requirements without touching the
    integrity of the financial and audit records (Section 3.18).
  - `audit_log` is deliberately generic, so an on-chain contract call and an
    off-chain AI inference reconstruct into one chronological view.

Types are kept portable — String rather than native UUID, JSON rather than
JSONB — so the same models run against PostgreSQL 15 in deployment and SQLite
in the test suite without a second schema to keep in step.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON,
    String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ enums --

class Role(str, enum.Enum):
    BUYER = "buyer"
    SELLER = "seller"
    ARBITRATOR = "arbitrator"
    ADMIN = "admin"


class EscrowState(str, enum.Enum):
    """Mirrors the Solidity enum. Ordinal order is load-bearing: the event
    listener decodes a uint8 from the chain and indexes into this sequence."""

    INITIATED = "INITIATED"
    DELIVERED = "DELIVERED"
    DISPUTED = "DISPUTED"
    COMPLETED = "COMPLETED"


class Outcome(str, enum.Enum):
    RELEASED_TO_SELLER = "released_to_seller"
    RESOLVED_FOR_SELLER = "resolved_for_seller"
    RESOLVED_FOR_BUYER = "resolved_for_buyer"
    TIMEOUT_REFUND = "timeout_refund"


class ChainStatus(str, enum.Enum):
    """How far an escrow has got with the chain.

    Kept separate from EscrowState because the two answer different questions.
    EscrowState is what the contract says the escrow *is*; ChainStatus is
    whether this record has managed to talk to the contract at all. An escrow
    can be INITIATED in the database and FAILED on chain, and conflating them
    would hide exactly that case.
    """

    DISABLED = "disabled"     # the chain layer is switched off
    PENDING = "pending"       # submitted, not yet confirmed
    CONFIRMED = "confirmed"   # on chain, receipt retained
    FAILED = "failed"         # submission or confirmation failed; see chain_error


class AuditLayer(str, enum.Enum):
    PRESENTATION = "presentation"
    APPLICATION = "application"
    ORACLE = "oracle"
    BLOCKCHAIN = "blockchain"


# ------------------------------------------------------------------ user ---

class User(Base):
    """The only table holding personally identifiable information."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    # Indexed: the oracle bridge and the event listener resolve an on-chain
    # address to an application identity on every event they process
    # (Section 3.9.2).
    wallet_address: Mapped[str] = mapped_column(String(42), unique=True, index=True)

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), index=True)
    display_name: Mapped[str] = mapped_column(String(120))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    bought: Mapped[list["Transaction"]] = relationship(
        back_populates="buyer", foreign_keys="Transaction.buyer_id",
    )
    sold: Mapped[list["Transaction"]] = relationship(
        back_populates="seller", foreign_keys="Transaction.seller_id",
    )


# ----------------------------------------------------------- transaction ---

class Transaction(Base):
    """Mirrors the escrow record held by EscrowContract.

    `state` is kept in step with the chain by the event listener, never written
    speculatively by a request handler — the chain is the authority and this is
    a projection of it.
    """

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    # The keccak256 identifier both layers compute independently (D-003).
    escrow_id: Mapped[str] = mapped_column(String(66), unique=True, index=True)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    arbitrator_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    amount_eth: Mapped[float] = mapped_column(Float)
    item_description: Mapped[str] = mapped_column(Text)

    state: Mapped[EscrowState] = mapped_column(
        Enum(EscrowState), default=EscrowState.INITIATED, index=True,
    )
    outcome: Mapped[Outcome | None] = mapped_column(Enum(Outcome), nullable=True)

    # Deposit nonce, part of the escrow id derivation.
    #
    # BigInteger, not Integer. The nonce is `secrets.randbits(48)`, so it
    # exceeds a 32-bit column for all but about one value in 131,072 — which
    # made escrow creation fail on PostgreSQL with `integer out of range`
    # while passing every test, because the suites run on SQLite and SQLite
    # stores integers at whatever width the value needs.
    #
    # 48 bits keeps the value below JavaScript's 2^53 safe-integer limit so the
    # browser can carry it without loss; 64 bits of column holds it with room
    # to spare. The on-chain parameter is a uint256, so the contract is not the
    # constraint here. See test_models.py::test_nonce_column_is_64_bit, which
    # asserts the type rather than the behaviour, because SQLite cannot
    # reproduce the overflow.
    nonce: Mapped[int] = mapped_column(BigInteger)

    # --- chain record -----------------------------------------------
    # What actually happened on Sepolia, as opposed to what this row claims.
    # Nullable throughout: the service runs with the chain switched off, and a
    # record written in that mode must not pretend to carry receipts.
    chain_status: Mapped[ChainStatus] = mapped_column(
        Enum(ChainStatus), default=ChainStatus.DISABLED, index=True,
    )

    # The value actually deposited, which is chain_demo_amount_eth and not
    # amount_eth. Stored so the two are never confused: amount_eth is the
    # notional figure the interface shows, this is what the contract holds.
    chain_amount_wei: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    score_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    deposit_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    settlement_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)

    # The block the deposit landed in. The event listener resumes from the
    # lowest such block rather than re-reading the chain from genesis.
    chain_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Why the chain write failed, kept rather than swallowed: a silent failure
    # here is a record that looks settled and is not.
    chain_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivery_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    holding_period_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    buyer: Mapped[User] = relationship(back_populates="bought", foreign_keys=[buyer_id])
    seller: Mapped[User] = relationship(back_populates="sold", foreign_keys=[seller_id])
    arbitrator: Mapped[User] = relationship(foreign_keys=[arbitrator_id])

    prediction: Mapped["FraudPrediction"] = relationship(
        back_populates="transaction", uselist=False,
    )
    dispute: Mapped["Dispute | None"] = relationship(
        back_populates="transaction", uselist=False,
    )


# ------------------------------------------------------- fraud prediction --

class FraudPrediction(Base):
    """One row per transaction, written at initiation.

    Section 3.9.2 requires the model version to be stored alongside the score so
    a historical prediction can be reproduced and audited after the deployed
    model is retrained. The threshold in force is stored for the same reason:
    an administrator moving the gate later must not change what a past decision
    meant.
    """

    __tablename__ = "fraud_predictions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id"), unique=True, index=True,
    )

    risk_score: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(64))
    threshold_at_scoring: Mapped[float] = mapped_column(Float)

    latency_ms: Mapped[int] = mapped_column(Integer)
    # True when the inference exceeded the NFR-01 budget. Recorded rather than
    # discarded, so the requirement can be evaluated from the data.
    exceeded_budget: Mapped[bool] = mapped_column(Boolean, default=False)

    # The eighteen-feature vector as scored, and the per-feature contributions
    # shown to the buyer and the arbitrator (FR-08).
    feature_vector: Mapped[dict] = mapped_column(JSON)
    contributions: Mapped[list] = mapped_column(JSON)

    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    transaction: Mapped[Transaction] = relationship(back_populates="prediction")


# --------------------------------------------------------------- dispute ---

class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reference: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.id"), unique=True, index=True,
    )
    arbitrator_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)

    raised_by_role: Mapped[Role] = mapped_column(Enum(Role))
    reason: Mapped[str] = mapped_column(Text)
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Mirrors resolveDispute's boolean: true releases to the seller.
    resolution_for_seller: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    transaction: Mapped[Transaction] = relationship(back_populates="dispute")
    arbitrator: Mapped[User] = relationship()


# ------------------------------------------------------------- audit log ---

class AuditLog(Base):
    """FR-09 and NFR-06.

    Intentionally generic. An AI inference, an oracle transmission and a
    contract call are all one row shape, which is what allows them to be
    reconstructed into a single chronological view rather than three logs a
    reader has to interleave by hand.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)

    transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, index=True,
    )

    layer: Mapped[AuditLayer] = mapped_column(Enum(AuditLayer), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)

    actor_address: Mapped[str | None] = mapped_column(String(42), nullable=True)

    # Present only for entries that produced an on-chain transaction.
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    gas_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True,
    )


# -------------------------------------------------------------- settings ---

class SystemSetting(Base):
    """Administrator-configurable parameters (System Administrator, Figure 3.2).

    Kept in the database rather than in environment configuration because
    Section 3.18 requires the threshold to be adjustable "without requiring
    changes to the deployed model code" — and because every change must land in
    the audit log with the acting address.
    """

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(42), nullable=True)
