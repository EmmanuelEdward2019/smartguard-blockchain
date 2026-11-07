"""Request and response models.

Section 3.15.2 requires all incoming payloads to be validated against strict
schema models before processing. `extra="forbid"` makes that literal: a payload
carrying an unexpected field is rejected rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .models import ChainStatus, AuditLayer, EscrowState, Outcome, Role


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Response models read snake_case attributes off the ORM and emit camelCase to
# the client. `validation_alias` rather than `alias` is what makes that
# asymmetry work: FastAPI serialises with by_alias=True, so a plain `alias`
# would push the database's naming out to the API as well.
ORM_OUT = ConfigDict(from_attributes=True, populate_by_name=True)


# ------------------------------------------------------------------ auth ---

class LoginRequest(Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class Principal(BaseModel):
    userId: str
    walletAddress: str
    role: Role
    displayName: str


class LoginResponse(BaseModel):
    accessToken: str
    tokenType: Literal["bearer"] = "bearer"
    expiresIn: int
    principal: Principal
    redirectTo: str


# ------------------------------------------------------------------ risk ---

class ScoringInput(Strict):
    """The eighteen-feature vector, plus the context needed to record the score.

    Bounds are enforced here rather than trusted from the client: a request
    claiming a counterparty reputation of 40 would otherwise reach the model and
    produce a confident, meaningless answer.
    """

    amountEth: float = Field(gt=0, le=1_000_000)
    sellerId: str
    accountAgeDays: int = Field(ge=0, le=36_500)
    velocity24h: int = Field(ge=0, le=10_000)
    counterpartyReputation: float = Field(ge=0, le=1)
    sellerDisputeRate: float = Field(ge=0, le=1)
    buyerDisputeRate: float = Field(ge=0, le=1)
    deviceConsistent: bool
    highRiskGeo: bool
    offHours: bool
    meanAmount30d: float = Field(ge=0)
    # The buyer's wallet balance before this transaction. Required because
    # `amount_to_account_ratio` is trained as amount / (balance + 1); computing
    # it from anything else at inference time feeds the model a different
    # quantity than it learned. See docs/DECISIONS.md D-012.
    accountBalanceEth: float = Field(ge=0)
    channel: Literal["web", "mobile"]


class Contribution(BaseModel):
    feature: str
    value: float | None
    importance: float


class RiskScoreResponse(BaseModel):
    riskScore: float
    modelVersion: str
    latencyMs: int
    exceededBudget: bool
    threshold: float
    withinGate: bool
    contributions: list[Contribution]
    source: Literal["model"] = "model"


# --------------------------------------------------------------- escrows ---

class CreateEscrowRequest(Strict):
    sellerId: str
    amountEth: float = Field(gt=0, le=1_000_000)
    itemDescription: str = Field(min_length=6, max_length=2000)
    scoring: ScoringInput


class PredictionOut(BaseModel):
    model_config = ConfigDict(**ORM_OUT, protected_namespaces=())

    riskScore: float = Field(validation_alias="risk_score")
    modelVersion: str = Field(validation_alias="model_version")
    thresholdAtScoring: float = Field(validation_alias="threshold_at_scoring")
    latencyMs: int = Field(validation_alias="latency_ms")
    exceededBudget: bool = Field(validation_alias="exceeded_budget")
    contributions: list[dict[str, Any]]
    predictedAt: datetime = Field(validation_alias="predicted_at")


class PartyOut(BaseModel):
    model_config = ORM_OUT

    id: str
    displayName: str = Field(validation_alias="display_name")
    walletAddress: str = Field(validation_alias="wallet_address")


class EscrowOut(BaseModel):
    model_config = ORM_OUT

    id: str
    reference: str
    escrowId: str = Field(validation_alias="escrow_id")
    amountEth: float = Field(validation_alias="amount_eth")
    itemDescription: str = Field(validation_alias="item_description")
    state: EscrowState
    outcome: Outcome | None
    createdAt: datetime = Field(validation_alias="created_at")
    deliveryConfirmedAt: datetime | None = Field(validation_alias="delivery_confirmed_at")
    completedAt: datetime | None = Field(validation_alias="completed_at")
    holdingPeriodEndsAt: datetime = Field(validation_alias="holding_period_ends_at")
    buyer: PartyOut
    seller: PartyOut
    arbitrator: PartyOut
    prediction: PredictionOut | None

    # --- what the chain says ------------------------------------------
    # Carried so the interface can distinguish an escrow the contract holds
    # from one only this database believes in. Without it a PENDING record and
    # a CONFIRMED one look identical to a reader.
    chainStatus: ChainStatus = Field(validation_alias="chain_status")
    chainAmountWei: int | None = Field(default=None, validation_alias="chain_amount_wei")
    scoreTxHash: str | None = Field(default=None, validation_alias="score_tx_hash")
    depositTxHash: str | None = Field(default=None, validation_alias="deposit_tx_hash")
    settlementTxHash: str | None = Field(default=None, validation_alias="settlement_tx_hash")
    chainBlock: int | None = Field(default=None, validation_alias="chain_block")
    chainError: str | None = Field(default=None, validation_alias="chain_error")

    # What this principal may do next. Computed per request, so the interface
    # never has to reimplement the policy to decide which controls to render.
    permittedActions: list[str] = []


# -------------------------------------------------------------- disputes ---

class RaiseDisputeRequest(Strict):
    reason: str = Field(min_length=20, max_length=4000)


class ResolveDisputeRequest(Strict):
    releaseToSeller: bool
    note: str = Field(min_length=20, max_length=4000)


class DisputeOut(BaseModel):
    model_config = ORM_OUT

    id: str
    reference: str
    transactionId: str = Field(validation_alias="transaction_id")
    raisedByRole: Role = Field(validation_alias="raised_by_role")
    reason: str
    raisedAt: datetime = Field(validation_alias="raised_at")
    resolutionForSeller: bool | None = Field(validation_alias="resolution_for_seller")
    resolutionNote: str | None = Field(validation_alias="resolution_note")
    resolvedAt: datetime | None = Field(validation_alias="resolved_at")


# ----------------------------------------------------------------- audit ---

class AuditOut(BaseModel):
    model_config = ORM_OUT

    id: str
    transactionId: str | None = Field(validation_alias="transaction_id")
    layer: AuditLayer
    eventType: str = Field(validation_alias="event_type")
    summary: str
    actorAddress: str | None = Field(validation_alias="actor_address")
    txHash: str | None = Field(validation_alias="tx_hash")
    gasUsed: int | None = Field(validation_alias="gas_used")
    blockNumber: int | None = Field(validation_alias="block_number")
    createdAt: datetime = Field(validation_alias="created_at")


# -------------------------------------------------------------- settings ---

class SettingsOut(BaseModel):
    riskThreshold: float
    timeLockHours: int
    holdingPeriodHours: int
    modelVersion: str
    failClosedOnInferenceError: bool


class SettingsUpdate(Strict):
    riskThreshold: float | None = Field(default=None, ge=0, le=1)
    timeLockHours: int | None = Field(default=None, ge=1, le=336)
    holdingPeriodHours: int | None = Field(default=None, ge=1, le=8760)
    failClosedOnInferenceError: bool | None = None


# ---------------------------------------------------------------- health ---

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    model: bool
    modelError: str | None
    modelVersion: str
    version: str
