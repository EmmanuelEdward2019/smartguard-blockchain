/**
 * The API-backed data source.
 *
 * lib/store.tsx has always said that swapping its mutations for real calls
 * "is then a change confined to store-core". This is that change: the twelve
 * components that read `useStore()` are untouched, because the shape they
 * consume has not moved. What has moved is where the rows come from.
 *
 * Everything goes through this application's own route handlers under
 * `/api/escrows`, never to FastAPI directly. Those handlers attach the bearer
 * token out of the HttpOnly session cookie, which is the whole reason the
 * browser cannot hold it.
 *
 * Detection is by probe rather than by configuration. `NEXT_PUBLIC_API_URL` is
 * deliberately empty — it is the switch that tells the interface to label
 * unbacked figures — so asking it whether a backend exists would always say
 * no. A single GET answers the question honestly, and a 401 or 503 falls back
 * to the local engine rather than showing an empty workspace as though there
 * were nothing to show.
 */

import type {
  Dispute, EscrowState, FeatureContribution, FraudPrediction, Outcome, Transaction,
} from './types';

interface ApiParty {
  id: string;
  displayName: string;
  walletAddress: string;
}

interface ApiPrediction {
  riskScore: number;
  modelVersion: string;
  thresholdAtScoring: number;
  latencyMs: number;
  exceededBudget: boolean;
  contributions: FeatureContribution[];
}

export interface ApiEscrow {
  id: string;
  reference: string;
  escrowId: string;
  amountEth: number;
  itemDescription: string;
  state: EscrowState;
  outcome: Outcome | null;
  createdAt: string;
  deliveryConfirmedAt: string | null;
  completedAt: string | null;
  holdingPeriodEndsAt: string;
  buyer: ApiParty;
  seller: ApiParty;
  arbitrator: ApiParty;
  prediction: ApiPrediction | null;
  chainStatus?: string;
  chainAmountWei?: number | null;
  scoreTxHash?: string | null;
  depositTxHash?: string | null;
  settlementTxHash?: string | null;
  chainBlock?: number | null;
  chainError?: string | null;
  permittedActions?: string[];
  events?: ApiEvent[];
}

interface ApiEvent {
  id: string;
  layer: string;
  eventType: string;
  summary: string;
  actorAddress: string | null;
  txHash: string | null;
  gasUsed: number | null;
  blockNumber: number | null;
  createdAt: string;
}

/**
 * The reference, not the surrogate key, is used as the client-side id.
 *
 * Routes are `/escrows/{id}` and the detail page passes that segment straight
 * through to the API, which looks escrows up by reference. Using the UUID here
 * would produce links that 404.
 */
export function toTransaction(row: ApiEscrow): Transaction {
  const prediction: FraudPrediction = {
    id: `pred-${row.reference}`,
    transactionId: row.reference,
    riskScore: row.prediction?.riskScore ?? 0,
    modelVersion: row.prediction?.modelVersion ?? 'unknown',
    thresholdAtScoring: row.prediction?.thresholdAtScoring ?? 0.75,
    latencyMs: row.prediction?.latencyMs ?? 0,
    predictedAt: row.createdAt,
    topContributions: row.prediction?.contributions ?? [],
  };

  return {
    id: row.reference,
    // The contract assigns no sequential id; the deposit's block is the
    // closest honest stand-in and is 0 until the deposit confirms.
    onChainId: row.chainBlock ?? 0,
    buyerId: row.buyer.id,
    sellerId: row.seller.id,
    arbitratorId: row.arbitrator.id,
    amountEth: row.amountEth,
    itemDescription: row.itemDescription,
    state: row.state,
    outcome: row.outcome ?? undefined,
    createdAt: row.createdAt,
    deliveryConfirmedAt: row.deliveryConfirmedAt ?? undefined,
    completedAt: row.completedAt ?? undefined,
    holdingPeriodEndsAt: row.holdingPeriodEndsAt,
    prediction,
    chainStatus: row.chainStatus as never,
    escrowIdHash: row.escrowId?.startsWith('0x') ? row.escrowId : undefined,
    scoreTxHash: row.scoreTxHash ?? undefined,
    depositTxHash: row.depositTxHash ?? undefined,
    settlementTxHash: row.settlementTxHash ?? undefined,
  };
}

/** Rows the API does not model separately, derived from the escrows it does. */
export function toDisputes(rows: ApiEscrow[]): Dispute[] {
  return rows
    .filter((row) => row.state === 'DISPUTED' || row.outcome?.startsWith('resolved'))
    .map((row) => ({
      id: `dis-${row.reference}`,
      transactionId: row.reference,
      raisedByRole: 'buyer' as const,
      reason: 'Raised through the application.',
      raisedAt: row.deliveryConfirmedAt ?? row.createdAt,
      arbitratorId: row.arbitrator.id,
      releasedToSeller: row.outcome === 'resolved_for_seller' ? true
        : row.outcome === 'resolved_for_buyer' ? false : undefined,
      resolvedAt: row.completedAt ?? undefined,
      resolutionNote: undefined,
    })) as Dispute[];
}

/**
 * Sellers the API knows about, gathered from the escrows it returned.
 *
 * The seller dropdown is populated from lib/seed with local ids like
 * `u-seller`, and the API looks sellers up by their database UUID, so the two
 * have to be reconciled somewhere. There is no users endpoint to ask, and
 * adding one to list every account to every buyer would be a worse trade than
 * deriving the handful of sellers already visible in this principal's own
 * escrows.
 *
 * The limitation is real and worth stating: a seller who has never traded
 * cannot be selected in API mode.
 */
export function knownSellers(rows: ApiEscrow[]): ApiParty[] {
  const seen = new Map<string, ApiParty>();
  for (const row of rows) {
    if (!seen.has(row.seller.id)) seen.set(row.seller.id, row.seller);
  }
  return [...seen.values()];
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Maps whatever the form supplied onto a seller the API will accept. */
export function resolveSellerId(
  supplied: string, displayName: string | undefined, sellers: ApiParty[],
): string | null {
  if (UUID.test(supplied)) return supplied;
  if (displayName) {
    const byName = sellers.find((s) => s.displayName === displayName);
    if (byName) return byName.id;
  }
  return sellers[0]?.id ?? null;
}

/* ------------------------------------------------------------ requests -- */

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message =
      body && typeof body === 'object' && 'error' in body
        ? String((body as { error: unknown }).error)
        : `Request failed (${response.status}).`;
    throw new ApiRequestError(message, response.status);
  }
  return body as T;
}

export class ApiRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'ApiRequestError';
  }
}

/** Every escrow this principal may see, or null when there is no backend. */
export async function fetchEscrows(): Promise<ApiEscrow[] | null> {
  try {
    return await request<ApiEscrow[]>('/api/escrows');
  } catch (error) {
    if (error instanceof ApiRequestError && (error.status === 401 || error.status === 503)) {
      return null;
    }
    throw error;
  }
}

/**
 * One escrow, including its event trail.
 *
 * The list endpoint omits events deliberately — returning the history of
 * every row would multiply a payload almost none of which is ever opened — so
 * the detail view asks for them when it needs them.
 */
export async function fetchEscrow(reference: string): Promise<ApiEscrow | null> {
  try {
    return await request<ApiEscrow>(`/api/escrows/${encodeURIComponent(reference)}`);
  } catch (error) {
    if (error instanceof ApiRequestError && (error.status === 401 || error.status === 503)) {
      return null;
    }
    throw error;
  }
}

export async function createEscrow(body: unknown): Promise<ApiEscrow> {
  return request<ApiEscrow>('/api/escrows', {
    method: 'POST', body: JSON.stringify(body),
  });
}

export async function postAction(
  reference: string, action: string, body?: unknown,
): Promise<unknown> {
  return request(`/api/escrows/${encodeURIComponent(reference)}/${action}`, {
    method: 'POST',
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}
