'use client';

/**
 * React binding for the data layer.
 *
 * The state itself lives in lib/store-core as a plain observable store, which
 * is read here through `useSyncExternalStore`. Mutations are exposed as async
 * functions because that is what they become once the FastAPI service answers
 * them — swapping the bodies for `api.*` calls is then a change confined to
 * store-core.
 */

import { useCallback, useMemo, useSyncExternalStore } from 'react';
import { useSession } from './auth/context';
import { requestRiskScore } from './api';
import { CONTRACTS, SELLER_PROFILES, SEED_NOW, USERS, USER_BY_ID } from './seed';
import * as core from './store-core';
import type { AuditEntry, Dispute, SystemSettings, Transaction } from './types';

export type NewEscrowInput = Omit<core.NewEscrowInput, 'buyerId'>;

interface StoreValue {
  transactions: Transaction[];
  disputes: Dispute[];
  audit: AuditEntry[];
  settings: SystemSettings;
  /** True once browser storage has been consulted. */
  ready: boolean;
  backendConnected: boolean;
  users: typeof USERS;
  contracts: typeof CONTRACTS;

  transactionById: (id: string) => Transaction | undefined;
  disputeForTransaction: (txId: string) => Dispute | undefined;
  auditForTransaction: (txId: string) => AuditEntry[];

  createEscrow: (input: NewEscrowInput) => Promise<Transaction>;
  confirmDelivery: (txId: string) => Promise<void>;
  releaseAfterTimeLock: (txId: string) => Promise<void>;
  initiateDispute: (txId: string, raisedByRole: 'buyer' | 'seller', reason: string) => Promise<void>;
  resolveDispute: (txId: string, forSeller: boolean, note: string) => Promise<void>;
  claimRefund: (txId: string) => Promise<void>;
  /** Fetches one escrow's event trail from the API, if one is connected. */
  loadEvents: (reference: string) => Promise<void>;
  updateSettings: (patch: Partial<SystemSettings>) => void;
  resetDemo: () => void;
}

export function useStore(): StoreValue {
  const { principal } = useSession();
  const state = useSyncExternalStore(core.subscribe, core.getSnapshot, core.getServerSnapshot);

  const transactionById = useCallback(
    (id: string) => state.transactions.find((t) => t.id === id),
    [state.transactions],
  );

  const disputeForTransaction = useCallback(
    (txId: string) => state.disputes.find((d) => d.transactionId === txId),
    [state.disputes],
  );

  const auditForTransaction = useCallback(
    (txId: string) => state.audit.filter((a) => a.transactionId === txId),
    [state.audit],
  );

  const createEscrow = useCallback(
    async (input: NewEscrowInput) => core.createEscrow({ ...input, buyerId: principal.userId }),
    [principal.userId],
  );

  const confirmDelivery = useCallback(async (txId: string) => core.confirmDelivery(txId), []);
  const releaseAfterTimeLock = useCallback(async (txId: string) => core.releaseAfterTimeLock(txId), []);
  const claimRefund = useCallback(async (txId: string) => core.claimRefund(txId), []);

  const initiateDispute = useCallback(
    async (txId: string, raisedByRole: 'buyer' | 'seller', reason: string) =>
      core.initiateDispute(txId, raisedByRole, reason),
    [],
  );

  const resolveDispute = useCallback(
    async (txId: string, forSeller: boolean, note: string) =>
      core.resolveDispute(txId, forSeller, note),
    [],
  );

  const updateSettings = useCallback(
    (patch: Partial<SystemSettings>) => core.updateSettings(patch, principal.walletAddress),
    [principal.walletAddress],
  );

  return useMemo(
    () => ({
      transactions: state.transactions,
      disputes: state.disputes,
      audit: state.audit,
      settings: state.settings,
      ready: state.hydrated,
      // Whether the API actually answered, not whether a build-time
      // variable was set. See the note on apiMode in store-core.
      backendConnected: state.apiConnected,
      users: USERS,
      contracts: CONTRACTS,
      transactionById,
      disputeForTransaction,
      auditForTransaction,
      createEscrow,
      confirmDelivery,
      releaseAfterTimeLock,
      initiateDispute,
      resolveDispute,
      claimRefund,
      loadEvents: core.loadEvents,
      updateSettings,
      resetDemo: core.resetStore,
    }),
    [
      state, transactionById, disputeForTransaction, auditForTransaction,
      createEscrow, confirmDelivery, releaseAfterTimeLock, initiateDispute,
      resolveDispute, claimRefund, updateSettings,
    ],
  );
}

export { requestRiskScore, SELLER_PROFILES, USER_BY_ID, SEED_NOW };
