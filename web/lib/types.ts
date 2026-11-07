/**
 * Domain model for the SmartGuard prototype.
 *
 * These types are the contract between the presentation layer and the
 * application/intelligence layer.  They mirror, field for field, the off-chain
 * PostgreSQL schema in Section 3.9.2 and the on-chain state machine in
 * Section 3.11.1, so the FastAPI service can serialise straight into them.
 */

/* ---------------------------------------------------------------- actors -- */

/** Stakeholder categories from Section 3.5.1. */
export type Role = 'buyer' | 'seller' | 'arbitrator' | 'admin';

/** `User` table — Section 3.9.2.  The only table holding PII. */
export interface User {
  id: string;
  walletAddress: string;
  role: Role;
  displayName: string;
  createdAt: string;
}

/* ------------------------------------------------- escrow state machine --- */

/**
 * The four states defined in Section 3.11.1.  Mirrors the Solidity enum in
 * EscrowContract, so the ordinal order matters: an event listener decoding a
 * `uint8` from the chain can index straight into `ESCROW_STATES`.
 */
export type EscrowState = 'INITIATED' | 'DELIVERED' | 'DISPUTED' | 'COMPLETED';

export const ESCROW_STATES: EscrowState[] = [
  'INITIATED',
  'DELIVERED',
  'DISPUTED',
  'COMPLETED',
];

/** How a COMPLETED escrow actually ended.  Off-chain label for the UI. */
export type Outcome =
  | 'released_to_seller'
  | 'refunded_to_buyer'
  | 'resolved_for_seller'
  | 'resolved_for_buyer'
  | 'timeout_refund';

export interface EscrowStateMeta {
  label: string;
  description: string;
  tone: 'chain' | 'safe' | 'elevated' | 'blocked';
}

export const ESCROW_STATE_META: Record<EscrowState, EscrowStateMeta> = {
  INITIATED: {
    label: 'Initiated',
    description:
      'Funds are held by EscrowContract. The seller is obligated to fulfil delivery within the holding period.',
    tone: 'chain',
  },
  DELIVERED: {
    label: 'Delivered',
    description:
      'Buyer confirmed receipt. A 48-hour time lock is running, during which either party may raise a dispute.',
    tone: 'elevated',
  },
  DISPUTED: {
    label: 'Disputed',
    description:
      'Decision authority has transferred to the designated arbitrator. Neither party can move the funds.',
    tone: 'blocked',
  },
  COMPLETED: {
    label: 'Completed',
    description:
      'Terminal state. Funds were released to the seller or refunded to the buyer.',
    tone: 'safe',
  },
};

/* --------------------------------------------------------------- risk ----- */

/**
 * Operating threshold enforced inside `depositFunds` (Sections 3.11.3, 3.15.1).
 * A score strictly above this reverts the deposit.
 *
 * Section 4.3.6 is explicit that 0.75 is a *prototype configuration value*, not
 * an empirically validated optimum — the UI must say so wherever it appears.
 */
export const DEFAULT_RISK_THRESHOLD = 0.75;

export type RiskBand = 'safe' | 'elevated' | 'high' | 'blocked';

export interface RiskBandMeta {
  label: string;
  /** Lower bound, inclusive. */
  min: number;
  tone: RiskBand;
  guidance: string;
}

/**
 * Bands are presentation-only. Only the `blocked` boundary is enforced on
 * chain; the rest exist to make the score legible to a non-technical user
 * (FR-08) without implying a precision the model does not have.
 */
export const RISK_BANDS: RiskBandMeta[] = [
  {
    label: 'Low',
    min: 0,
    tone: 'safe',
    guidance: 'Consistent with the counterparty history in the feature vector.',
  },
  {
    label: 'Elevated',
    min: 0.4,
    tone: 'elevated',
    guidance: 'Some features deviate from the account baseline. Deposit proceeds.',
  },
  {
    label: 'High',
    min: 0.6,
    tone: 'high',
    guidance: 'Close to the on-chain gate. Review the contributing features before depositing.',
  },
  {
    label: 'Blocked',
    min: DEFAULT_RISK_THRESHOLD,
    tone: 'blocked',
    guidance: 'Above the configured gate. depositFunds will revert; no funds can be committed.',
  },
];

export function riskBand(score: number, threshold = DEFAULT_RISK_THRESHOLD): RiskBandMeta {
  if (score > threshold) return RISK_BANDS[3];
  // Walk down so the highest matching band wins.
  for (let i = RISK_BANDS.length - 2; i >= 0; i--) {
    if (score >= RISK_BANDS[i].min) return RISK_BANDS[i];
  }
  return RISK_BANDS[0];
}

/** One feature's contribution to a single scoring event. */
export interface FeatureContribution {
  /** Key into FEATURE_SET — see lib/features.ts */
  feature: string;
  /** The value the model actually saw, already formatted for display. */
  value: string;
  /** Signed push toward (positive) or away from (negative) the fraud class. */
  contribution: number;
}

/**
 * `FraudPrediction` table — Section 3.9.2.  One row per transaction, retaining
 * the model version so a historical score stays auditable after a retrain.
 */
export interface FraudPrediction {
  id: string;
  transactionId: string;
  /** P(fraud), 0–1, as returned by the Random Forest inference service. */
  riskScore: number;
  modelVersion: string;
  /** Threshold in force at scoring time. Stored, not looked up, so the record
   *  stays interpretable after an administrator changes the setting. */
  thresholdAtScoring: number;
  /** Wall-clock milliseconds for the inference call. NFR-01 budget is 2000ms. */
  latencyMs: number;
  predictedAt: string;
  topContributions: FeatureContribution[];
}

/* --------------------------------------------------------- transactions --- */

/** `Transaction` table — Section 3.9.2.  `id` mirrors the on-chain escrow id. */
export interface Transaction {
  id: string;
  /** Sequential id assigned by EscrowContract. */
  onChainId: number;
  buyerId: string;
  sellerId: string;
  arbitratorId: string;
  /** Denominated in Ether, matching the contract's `msg.value`. */
  amountEth: number;
  itemDescription: string;
  state: EscrowState;
  outcome?: Outcome;
  createdAt: string;
  /** Set when confirmDelivery lands; start of the 48-hour time lock. */
  deliveryConfirmedAt?: string;
  completedAt?: string;
  /** Deadline for claimRefund (FR-06). */
  holdingPeriodEndsAt: string;
  prediction: FraudPrediction;

  /**
   * What the chain holds for this escrow, when the API supplied it.
   *
   * Absent on locally generated records, which is the distinction the
   * interface needs: a record with `confirmed` carries real transaction
   * hashes, one without carries none and must not be presented as though it
   * did.
   */
  chainStatus?: 'disabled' | 'pending' | 'confirmed' | 'failed';
  escrowIdHash?: string;
  scoreTxHash?: string;
  depositTxHash?: string;
  settlementTxHash?: string;
}

/** `Dispute` table — Section 3.9.2. */
export interface Dispute {
  id: string;
  transactionId: string;
  raisedByRole: Extract<Role, 'buyer' | 'seller'>;
  reason: string;
  raisedAt: string;
  arbitratorId: string;
  /** true → release to seller, false → refund to buyer. Mirrors the boolean
   *  parameter of `resolveDispute` (Section 3.11.3). */
  resolutionForSeller?: boolean;
  resolutionNote?: string;
  resolvedAt?: string;
}

/* ------------------------------------------------------------- audit ------ */

/**
 * `AuditLog` table — Section 3.9.2.  Deliberately generic so that on-chain and
 * off-chain events reconstruct into one chronological view (FR-09, NFR-06).
 */
export type AuditLayer =
  | 'presentation'
  | 'application'
  | 'oracle'
  | 'blockchain';

export interface AuditEntry {
  id: string;
  transactionId?: string;
  layer: AuditLayer;
  /** e.g. 'ai.inference', 'oracle.updateRiskScore', 'contract.depositFunds' */
  eventType: string;
  actorAddress?: string;
  summary: string;
  /** Present only for entries that produced an on-chain transaction. */
  txHash?: string;
  gasUsed?: number;
  blockNumber?: number;
  createdAt: string;
}

export interface AuditLayerMeta {
  label: string;
  short: string;
  tone: 'brand' | 'chain' | 'neutral';
}

export const AUDIT_LAYER_META: Record<AuditLayer, AuditLayerMeta> = {
  presentation:  { label: 'Presentation layer',              short: 'UI',      tone: 'neutral' },
  application:   { label: 'Application & intelligence layer', short: 'API/ML',  tone: 'brand'   },
  oracle:        { label: 'Oracle / integration layer',       short: 'Oracle',  tone: 'chain'   },
  blockchain:    { label: 'Blockchain layer',                 short: 'Chain',   tone: 'chain'   },
};

/* ------------------------------------------------- contract call surface -- */

/** The five EscrowContract entry points plus the oracle write (Section 3.11.3). */
export type ContractFunction =
  | 'depositFunds'
  | 'confirmDelivery'
  | 'initiateDispute'
  | 'resolveDispute'
  | 'claimRefund'
  | 'updateRiskScore';

/**
 * Mean gas figures reported in Table 4.2.  Chapter Four labels these as
 * descriptive estimates without retained execution receipts, so anywhere they
 * surface in the UI they must be marked as such — never as measurements.
 */
/**
 * Gas measured on Sepolia, from docs/evidence/sepolia-measurements.json.
 *
 * These replace the estimates the report's Table 4.2 carried. Two of the
 * figures could not be taken live — releaseFunds and claimRefund sit behind a
 * 48-hour and a seven-day time lock, and a public chain's clock cannot be
 * advanced — so those come from the local harness and are marked as such.
 *
 * Do not average the two sources. The same bytecode is priced differently:
 * Sepolia charges a large surcharge for storage that was previously zero, so
 * operations that allocate state cost three to four times the local figure
 * while those that only overwrite agree to within four per cent.
 */
export const REPORTED_GAS: Record<ContractFunction, number> = {
  depositFunds:    595_275,
  updateRiskScore: 365_109,
  resolveDispute:   62_273,
  confirmDelivery:  38_061,
  initiateDispute:  37_802,
  claimRefund:      44_801,   // local harness; behind the seven-day hold
};

/* ------------------------------------------------------- system settings -- */

/** Administrator-configurable parameters (System Administrator actor, Fig 3.2). */
export interface SystemSettings {
  riskThreshold: number;
  /** Hours the funds stay claimable-back by the buyer in INITIATED (FR-06). */
  holdingPeriodHours: number;
  /** Post-delivery dispute window, 48h in Section 3.11.1. */
  timeLockHours: number;
  modelVersion: string;
  /** Graceful degradation, Section 3.7.1: route to manual review when the
   *  inference service is unreachable rather than blocking all activity. */
  failClosedOnInferenceError: boolean;
}

export const DEFAULT_SETTINGS: SystemSettings = {
  riskThreshold: DEFAULT_RISK_THRESHOLD,
  holdingPeriodHours: 168,
  timeLockHours: 48,
  modelVersion: 'rf-paysim-v1.0.0',
  failClosedOnInferenceError: true,
};
