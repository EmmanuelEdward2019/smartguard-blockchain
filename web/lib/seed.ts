/**
 * Seeded demonstration dataset.
 *
 * Every value here is derived from a fixed seed, so the interface shows the
 * same figures on every machine and in every screenshot. Nothing in this file
 * is a measurement; it exists so the four workflows (initiation, delivery,
 * dispute, refund) can be walked end to end before the backend is connected.
 */

import { fakeAddress, fakeTxHash, gasFor, scoreTransaction, type ScoringInput } from './engine';
import {
  DEFAULT_RISK_THRESHOLD,
  REPORTED_GAS,
  type AuditEntry,
  type Dispute,
  type EscrowState,
  type FraudPrediction,
  type Outcome,
  type Transaction,
  type User,
} from './types';

export const MODEL_VERSION = 'rf-paysim-v1.0.0';

/** Deployed contract addresses. Placeholders until a real Sepolia deployment
 *  replaces them — Appendix A is explicit that the real identifiers must be
 *  copied from genuine deployment records before any verification claim. */
/**
 * The deployed contracts, from docs/evidence/deployment.sepolia.json.
 *
 * Real addresses, not generated ones: both are verified on Sepolia Etherscan,
 * so anything the interface shows here can be checked against the chain. They
 * were placeholders while nothing was deployed; keeping them after a
 * deployment would mean the interface disagreed with the evidence record.
 */
export const CONTRACTS = {
  escrow: '0x02f7AF12B2Bcf967E9439acea560B60491B57CCD',
  riskOracle: '0x7E7Dc2C906ab520097c57B747f0d5b406E6a9aEe',
  oracleOperator: '0x58227C0252DeD8cC66Ff7a7B678992C50DEbd954',
  network: 'Ethereum Sepolia',
  chainId: 11155111,
} as const;

/* --------------------------------------------------------------- users ---- */

export const USERS: User[] = [
  { id: 'u-buyer',   walletAddress: fakeAddress('buyer-amina'),  role: 'buyer',      displayName: 'Amina Yusuf',      createdAt: '2026-03-04T09:12:00Z' },
  { id: 'u-seller',  walletAddress: fakeAddress('seller-tunde'), role: 'seller',     displayName: 'Tunde Okafor',     createdAt: '2026-01-19T14:03:00Z' },
  { id: 'u-seller2', walletAddress: fakeAddress('seller-grace'), role: 'seller',     displayName: 'Grace Adeyemi',    createdAt: '2026-08-27T11:41:00Z' },
  { id: 'u-seller3', walletAddress: fakeAddress('seller-kola'),  role: 'seller',     displayName: 'Kola Ibrahim',     createdAt: '2025-11-02T08:20:00Z' },
  { id: 'u-arb',     walletAddress: fakeAddress('arbitrator'),   role: 'arbitrator', displayName: 'Dr Ngozi Eze',     createdAt: '2025-09-15T10:00:00Z' },
  { id: 'u-admin',   walletAddress: fakeAddress('admin'),        role: 'admin',      displayName: 'System Operator',  createdAt: '2025-09-01T00:00:00Z' },
];

export const USER_BY_ID: Record<string, User> = Object.fromEntries(USERS.map((u) => [u.id, u]));

/**
 * Adds accounts the API reported to the directory components look names up in.
 *
 * The seeded accounts carry local ids like `u-seller`; the API identifies the
 * same people by database UUID. Without this the escrow table renders every
 * counterparty as an em dash, because the lookup misses.
 *
 * It mutates the exported record rather than replacing it, since the modules
 * that read it hold the original reference.
 */
export function registerParties(
  parties: { id: string; displayName: string; walletAddress: string }[],
): void {
  for (const party of parties) {
    if (USER_BY_ID[party.id]) continue;
    USER_BY_ID[party.id] = {
      id: party.id,
      walletAddress: party.walletAddress,
      // The role is not carried on an escrow's party, and nothing reads it
      // for a counterparty — only the name and the address are shown.
      role: 'seller',
      displayName: party.displayName,
      createdAt: new Date(0).toISOString(),
    } as User;
  }
}

/** Behavioural history per seller, feeding the reputation-derived features. */
export const SELLER_PROFILES: Record<string, { reputation: number; disputeRate: number; completed: number }> = {
  'u-seller':  { reputation: 0.974, disputeRate: 0.021, completed: 143 },
  'u-seller2': { reputation: 0.610, disputeRate: 0.180, completed: 11  },
  'u-seller3': { reputation: 0.902, disputeRate: 0.064, completed: 78  },
};

/* ------------------------------------------------------------ timestamps -- */

/** Fixed clock origin so relative timestamps stay stable across renders. */
export const SEED_NOW = new Date('2026-09-09T10:30:00Z').getTime();

const hoursAgo = (h: number) => new Date(SEED_NOW - h * 3_600_000).toISOString();
const hoursAhead = (h: number) => new Date(SEED_NOW + h * 3_600_000).toISOString();

/* --------------------------------------------------------- transactions -- */

interface SeedSpec {
  id: string;
  onChainId: number;
  sellerId: string;
  amountEth: number;
  item: string;
  state: EscrowState;
  outcome?: Outcome;
  createdHoursAgo: number;
  deliveredHoursAgo?: number;
  completedHoursAgo?: number;
  input: Partial<ScoringInput>;
}

const SPECS: SeedSpec[] = [
  {
    id: 'ESC-1042', onChainId: 42, sellerId: 'u-seller', amountEth: 1.85,
    item: 'Refurbished MacBook Pro 14" (M3, 512GB)',
    state: 'INITIATED', createdHoursAgo: 6,
    input: { velocity24h: 1, accountAgeDays: 189, meanAmount30d: 1.6 },
  },
  {
    id: 'ESC-1041', onChainId: 41, sellerId: 'u-seller3', amountEth: 0.42,
    item: 'Industrial sewing machine — spare parts lot',
    state: 'DELIVERED', createdHoursAgo: 52, deliveredHoursAgo: 9,
    input: { velocity24h: 2, accountAgeDays: 189, meanAmount30d: 1.6 },
  },
  {
    id: 'ESC-1040', onChainId: 40, sellerId: 'u-seller2', amountEth: 4.60,
    item: 'Bulk order — 40× solar inverter units',
    state: 'DISPUTED', createdHoursAgo: 96, deliveredHoursAgo: 30,
    // Elevated but admitted: 0.67 against a 0.75 gate. This is the case the
    // gate let through and that then went wrong, which is the honest story —
    // a threshold is a trade-off, not a guarantee.
    input: { velocity24h: 3, accountAgeDays: 189, meanAmount30d: 1.6, highRiskGeo: true },
  },
  {
    id: 'ESC-1039', onChainId: 39, sellerId: 'u-seller', amountEth: 2.10,
    item: 'Canon EOS R6 body + 24-105mm lens',
    state: 'COMPLETED', outcome: 'released_to_seller',
    createdHoursAgo: 168, deliveredHoursAgo: 122, completedHoursAgo: 74,
    input: { velocity24h: 1, accountAgeDays: 182, meanAmount30d: 1.6 },
  },
  {
    id: 'ESC-1038', onChainId: 38, sellerId: 'u-seller2', amountEth: 3.25,
    item: 'Wholesale textile consignment (unverified seller)',
    state: 'COMPLETED', outcome: 'resolved_for_buyer',
    createdHoursAgo: 240, deliveredHoursAgo: 190, completedHoursAgo: 140,
    input: { velocity24h: 6, accountAgeDays: 176, meanAmount30d: 1.6, highRiskGeo: true, offHours: true },
  },
  {
    id: 'ESC-1037', onChainId: 37, sellerId: 'u-seller3', amountEth: 0.88,
    item: 'Server rack UPS — 3000VA',
    state: 'COMPLETED', outcome: 'timeout_refund',
    createdHoursAgo: 420, completedHoursAgo: 250,
    input: { velocity24h: 2, accountAgeDays: 168, meanAmount30d: 1.6 },
  },
];

function buildInput(spec: SeedSpec): ScoringInput {
  const profile = SELLER_PROFILES[spec.sellerId];
  const seller = USER_BY_ID[spec.sellerId];
  const buyer = USER_BY_ID['u-buyer'];
  return {
    amountEth: spec.amountEth,
    sellerAddress: seller.walletAddress,
    buyerAddress: buyer.walletAddress,
    accountAgeDays: 189,
    velocity24h: 1,
    counterpartyReputation: profile.reputation,
    sellerDisputeRate: profile.disputeRate,
    buyerDisputeRate: 0.014,
    deviceConsistent: true,
    highRiskGeo: false,
    offHours: false,
    meanAmount30d: 1.6,
    channel: 'web',
    ...spec.input,
  };
}

function buildPrediction(spec: SeedSpec): FraudPrediction {
  const result = scoreTransaction(buildInput(spec));
  return {
    id: `pred-${spec.id}`,
    transactionId: spec.id,
    riskScore: result.riskScore,
    modelVersion: MODEL_VERSION,
    thresholdAtScoring: 0.75,
    latencyMs: result.latencyMs,
    predictedAt: hoursAgo(spec.createdHoursAgo),
    topContributions: result.contributions,
  };
}

/**
 * Invariant: an escrow that reached the chain must have cleared the gate in
 * force when it was scored. A seeded record above the threshold would
 * contradict its own audit trail, so it is caught here rather than shipped.
 */
function assertAdmitted(transactions: Transaction[]): Transaction[] {
  const gate = DEFAULT_RISK_THRESHOLD;
  const impossible = transactions.filter((t) => t.prediction.riskScore > gate);
  if (impossible.length > 0) {
    const detail = impossible
      .map((t) => `${t.id} scored ${t.prediction.riskScore.toFixed(4)}`)
      .join(', ');
    throw new Error(
      `Seed data is inconsistent: ${detail} exceeds the ${gate} risk gate, so depositFunds would have reverted and the escrow could not exist.`,
    );
  }
  return transactions;
}

export const SEED_TRANSACTIONS: Transaction[] = assertAdmitted(SPECS.map((spec) => ({
  id: spec.id,
  onChainId: spec.onChainId,
  buyerId: 'u-buyer',
  sellerId: spec.sellerId,
  arbitratorId: 'u-arb',
  amountEth: spec.amountEth,
  itemDescription: spec.item,
  state: spec.state,
  outcome: spec.outcome,
  createdAt: hoursAgo(spec.createdHoursAgo),
  deliveryConfirmedAt: spec.deliveredHoursAgo != null ? hoursAgo(spec.deliveredHoursAgo) : undefined,
  completedAt: spec.completedHoursAgo != null ? hoursAgo(spec.completedHoursAgo) : undefined,
  holdingPeriodEndsAt: hoursAhead(168 - spec.createdHoursAgo),
  prediction: buildPrediction(spec),
})));

/* -------------------------------------------------------------- disputes -- */

export const SEED_DISPUTES: Dispute[] = [
  {
    id: 'DSP-204',
    transactionId: 'ESC-1040',
    raisedByRole: 'buyer',
    reason:
      'Consignment arrived 12 units short and three inverters were the wrong model. Seller has not responded to two messages since delivery was marked.',
    raisedAt: hoursAgo(26),
    arbitratorId: 'u-arb',
  },
  {
    id: 'DSP-198',
    transactionId: 'ESC-1038',
    raisedByRole: 'buyer',
    reason: 'Tracking record showed delivery to a different state. No goods were received.',
    raisedAt: hoursAgo(180),
    arbitratorId: 'u-arb',
    resolutionForSeller: false,
    resolutionNote:
      'Courier record does not place the consignment at the buyer address. Refund to buyer.',
    resolvedAt: hoursAgo(140),
  },
];

/* ----------------------------------------------------------- audit trail -- */

let auditCounter = 0;
const nextAuditId = () => `aud-${String(++auditCounter).padStart(4, '0')}`;

function chainEntry(
  txId: string,
  fn: keyof typeof REPORTED_GAS,
  summary: string,
  actor: string,
  hoursAgoValue: number,
  block: number,
): AuditEntry {
  return {
    id: nextAuditId(),
    transactionId: txId,
    layer: fn === 'updateRiskScore' ? 'oracle' : 'blockchain',
    eventType: `contract.${fn}`,
    actorAddress: actor,
    summary,
    txHash: fakeTxHash(`${txId}:${fn}`),
    gasUsed: gasFor(fn, REPORTED_GAS[fn], txId),
    blockNumber: block,
    createdAt: hoursAgo(hoursAgoValue),
  };
}

/** Builds the four-layer trace every escrow initiation produces. */
function initiationTrace(tx: Transaction, hoursAgoValue: number, block: number): AuditEntry[] {
  const buyer = USER_BY_ID[tx.buyerId];
  return [
    {
      id: nextAuditId(), transactionId: tx.id, layer: 'presentation',
      eventType: 'ui.escrow.submit',
      actorAddress: buyer.walletAddress,
      summary: `Buyer submitted an escrow request for ${tx.amountEth} ETH.`,
      createdAt: hoursAgo(hoursAgoValue + 0.02),
    },
    {
      id: nextAuditId(), transactionId: tx.id, layer: 'application',
      eventType: 'ai.inference',
      summary: `Random Forest returned P(fraud) = ${tx.prediction.riskScore.toFixed(4)} in ${tx.prediction.latencyMs} ms (model ${tx.prediction.modelVersion}).`,
      createdAt: hoursAgo(hoursAgoValue + 0.015),
    },
    chainEntry(tx.id, 'updateRiskScore',
      `Oracle wrote risk score ${Math.round(tx.prediction.riskScore * 100)} to RiskOracleContract for escrow #${tx.onChainId}.`,
      CONTRACTS.oracleOperator, hoursAgoValue + 0.01, block),
    chainEntry(tx.id, 'depositFunds',
      `${tx.amountEth} ETH locked. Risk gate passed: ${Math.round(tx.prediction.riskScore * 100)} ≤ 75. State → INITIATED.`,
      buyer.walletAddress, hoursAgoValue, block + 1),
  ];
}

function buildAuditLog(): AuditEntry[] {
  const entries: AuditEntry[] = [];
  let block = 7_412_880;

  for (const spec of [...SPECS].reverse()) {
    const tx = SEED_TRANSACTIONS.find((t) => t.id === spec.id)!;
    const seller = USER_BY_ID[tx.sellerId];
    const buyer = USER_BY_ID[tx.buyerId];
    const arb = USER_BY_ID[tx.arbitratorId];

    entries.push(...initiationTrace(tx, spec.createdHoursAgo, block));
    block += 240;

    if (spec.deliveredHoursAgo != null) {
      entries.push(chainEntry(tx.id, 'confirmDelivery',
        'Buyer confirmed receipt. 48-hour time lock started.',
        buyer.walletAddress, spec.deliveredHoursAgo, block));
      block += 180;
    }

    if (tx.state === 'DISPUTED' || tx.outcome?.startsWith('resolved')) {
      const dispute = SEED_DISPUTES.find((d) => d.transactionId === tx.id);
      if (dispute) {
        entries.push(chainEntry(tx.id, 'initiateDispute',
          `Dispute raised by ${dispute.raisedByRole}. Decision authority transferred to arbitrator.`,
          buyer.walletAddress, (SEED_NOW - Date.parse(dispute.raisedAt)) / 3_600_000, block));
        block += 90;

        if (dispute.resolvedAt) {
          entries.push(chainEntry(tx.id, 'resolveDispute',
            `Arbitrator resolved in favour of the ${dispute.resolutionForSeller ? 'seller' : 'buyer'}. Funds ${dispute.resolutionForSeller ? 'released' : 'refunded'}. State → COMPLETED.`,
            arb.walletAddress, (SEED_NOW - Date.parse(dispute.resolvedAt)) / 3_600_000, block));
          block += 120;
        }
      }
    }

    if (tx.outcome === 'released_to_seller' && spec.completedHoursAgo != null) {
      entries.push(chainEntry(tx.id, 'confirmDelivery',
        `Time lock elapsed without dispute. ${tx.amountEth} ETH released to ${seller.displayName}. State → COMPLETED.`,
        seller.walletAddress, spec.completedHoursAgo, block));
      block += 150;
    }

    if (tx.outcome === 'timeout_refund' && spec.completedHoursAgo != null) {
      entries.push(chainEntry(tx.id, 'claimRefund',
        `Holding period elapsed with no delivery confirmation. ${tx.amountEth} ETH refunded to buyer. State → COMPLETED.`,
        buyer.walletAddress, spec.completedHoursAgo, block));
      block += 150;
    }
  }

  // A rejected initiation: scored above the gate, so no funds were ever committed.
  entries.push(
    {
      id: nextAuditId(), layer: 'presentation', eventType: 'ui.escrow.submit',
      actorAddress: USER_BY_ID['u-buyer'].walletAddress,
      summary: 'Buyer submitted an escrow request for 6.4 ETH to an unverified seller.',
      createdAt: hoursAgo(3.1),
    },
    {
      id: nextAuditId(), layer: 'application', eventType: 'ai.inference',
      summary: 'Random Forest returned P(fraud) = 0.8134 in 486 ms. Above the configured gate of 0.75.',
      createdAt: hoursAgo(3.08),
    },
    {
      id: nextAuditId(), layer: 'application', eventType: 'risk.gate.reject',
      summary: 'Initiation refused before any on-chain call. No gas consumed, no funds committed.',
      createdAt: hoursAgo(3.07),
    },
  );

  return entries.sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}

export const SEED_AUDIT: AuditEntry[] = buildAuditLog();
