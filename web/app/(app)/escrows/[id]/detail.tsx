'use client';

import { useEffect } from 'react';

import Link from 'next/link';
import { ArrowLeft, Boxes, Fingerprint, Fuel, Gavel, ShieldX, Timer } from 'lucide-react';
import { Address, Badge, Card, Empty, KeyValue, Notice, ui } from '@/components/ui';
import { AuditTimeline, ContributionBars, RiskBar, RiskDial, StateMachine } from '@/components/domain';
import { StateBadge } from '@/components/escrow';
import { EscrowActions } from '@/components/escrow-actions';
import { useSession } from '@/lib/auth/context';
import { canReadEscrow } from '@/lib/auth/policy';
import { useStore } from '@/lib/store';
import { useNow } from '@/lib/clock';
import { USER_BY_ID } from '@/lib/seed';
import { absoluteTime, duration, eth, gas, relativeTime } from '@/lib/format';

import s from './detail.module.css';

export function EscrowDetail({ id }: { id: string }) {
  const { principal } = useSession();
  const {
    transactionById, disputeForTransaction, auditForTransaction, settings, contracts, ready,
    loadEvents,
  } = useStore();
  const now = useNow();

  // The list endpoint omits event trails, so the one for this escrow is
  // fetched when the page opens. A failure is not surfaced: the rest of the
  // record is still worth showing, and the trail renders empty.
  useEffect(() => {
    void loadEvents(id).catch(() => {});
  }, [id, loadEvents, ready]);

  const tx = transactionById(id);

  // The store hydrates from storage on mount; hold the frame until it has.
  if (!tx) {
    if (!ready) return null;
    return (
      <Card>
        <Empty icon={<ShieldX size={19} />} title="Escrow not found">
          No escrow with the reference <code className="mono">{id}</code> is visible to your account.
        </Empty>
      </Card>
    );
  }

  // Object-level authorisation: being signed in is not enough — the principal
  // must be a party to this escrow, or hold read-any oversight.
  if (!canReadEscrow(principal, tx)) {
    return (
      <Card>
        <Empty icon={<ShieldX size={19} />} title="Not authorised">
          This escrow does not name your address, and your role does not grant oversight of escrows
          you are not party to.
        </Empty>
      </Card>
    );
  }

  const dispute = disputeForTransaction(tx.id);
  const entries = auditForTransaction(tx.id);
  const buyer = USER_BY_ID[tx.buyerId];
  const seller = USER_BY_ID[tx.sellerId];
  const arbitrator = USER_BY_ID[tx.arbitratorId];

  const totalGas = entries.reduce((sum, e) => sum + (e.gasUsed ?? 0), 0);

  const lockEnds = tx.deliveryConfirmedAt
    ? Date.parse(tx.deliveryConfirmedAt) + settings.timeLockHours * 3_600_000
    : null;
  const lockRemaining = lockEnds && now > 0 ? lockEnds - now : 0;
  const lockProgress = lockEnds && tx.deliveryConfirmedAt && now > 0
    ? Math.min(100, Math.max(0,
        ((now - Date.parse(tx.deliveryConfirmedAt)) / (settings.timeLockHours * 3_600_000)) * 100))
    : 0;

  const holdingRemaining = now > 0 ? Date.parse(tx.holdingPeriodEndsAt) - now : 0;

  const initials = (name: string) => name.split(' ').map((w) => w[0]).slice(0, 2).join('');

  const parties = [
    { label: 'Buyer', user: buyer, icon: null },
    { label: 'Seller', user: seller, icon: null },
    { label: 'Arbitrator', user: arbitrator, icon: <Gavel size={14} /> },
  ];

  return (
    <>
      <div className={s.head}>
        <div>
          <Link href="/escrows" className={s.back}>
            <ArrowLeft size={13} /> All escrows
          </Link>
          <h1 className={s.ref}>
            <span className="mono">{tx.id}</span>
            <StateBadge tx={tx} />
          </h1>
          <p className={s.item}>{tx.itemDescription}</p>
        </div>
        <div className={s.amount}>
          <div className={s.amountValue}>{eth(tx.amountEth)}</div>
          <div className={s.amountLabel}>
            {tx.state === 'COMPLETED' ? 'Settled' : 'Held by EscrowContract'}
          </div>
        </div>
      </div>

      <div className={s.grid}>
        <div className={s.column}>
          <Card title="Lifecycle" subtitle={`On-chain escrow #${tx.onChainId} · ${contracts.network}`}>
            <StateMachine
              state={tx.state}
              timestamps={{
                INITIATED: tx.createdAt,
                DELIVERED: tx.deliveryConfirmedAt,
                DISPUTED: dispute?.raisedAt,
                COMPLETED: tx.completedAt,
              }}
            />

            {tx.state === 'DELIVERED' && lockEnds && (
              <div className={s.lockBar} style={{ marginTop: 16 }}>
                <Timer size={16} style={{ color: 'var(--elevated)', flex: 'none' }} />
                <div style={{ flex: 1 }}>
                  <div className={s.lockLabel}>
                    Time lock running. Either party may still raise a dispute; after it elapses the
                    contract releases to {seller.displayName}.
                  </div>
                  <div className={s.lockTrack}>
                    <div className={s.lockFill} style={{ width: `${lockProgress}%` }} />
                  </div>
                </div>
                <span className={s.lockTime}>{duration(lockRemaining)}</span>
              </div>
            )}

            {tx.state === 'INITIATED' && now > 0 && (
              <div style={{ marginTop: 16 }}>
                <Notice tone={holdingRemaining > 0 ? 'chain' : 'warn'} icon={<Timer size={15} />}>
                  {holdingRemaining > 0
                    ? <>Holding period ends in <strong>{duration(holdingRemaining)}</strong>. If the buyer has neither confirmed nor disputed by then, <code className="mono">claimRefund</code> becomes available.</>
                    : <>The holding period has elapsed. The buyer may call <code className="mono">claimRefund</code> to recover the funds (FR-06).</>}
                </Notice>
              </div>
            )}

            <div className={s.actionBar} style={{ marginTop: 16 }}>
              <span className={s.actionLabel}>
                Actions your address is permitted to make in this state
              </span>
              <EscrowActions tx={tx} />
            </div>
          </Card>

          {dispute && (
            <Card
              title={`Dispute ${dispute.id}`}
              subtitle={`Raised by the ${dispute.raisedByRole} ${relativeTime(dispute.raisedAt)}`}
              actions={dispute.resolvedAt
                ? <Badge tone="safe" dot>Resolved</Badge>
                : <Badge tone="blocked" dot>Open</Badge>}
            >
              <div className={ui.stack}>
                <div>
                  <div className={s.sectionLabel}>Stated grounds</div>
                  <p className={s.prose}>{dispute.reason}</p>
                </div>

                {dispute.resolvedAt && (
                  <>
                    <hr className={ui.divider} />
                    <div>
                      <div className={s.sectionLabel}>
                        Ruling · {dispute.resolutionForSeller ? 'released to seller' : 'refunded to buyer'}
                      </div>
                      <p className={s.prose}>{dispute.resolutionNote}</p>
                      <p className={s.proseMeta}>
                        {arbitrator.displayName} · {absoluteTime(dispute.resolvedAt)}
                      </p>
                    </div>
                  </>
                )}
              </div>
            </Card>
          )}

          <Card
            title="Risk assessment"
            subtitle={`Recorded at initiation by model ${tx.prediction.modelVersion}. This is the value the oracle wrote on chain.`}
          >
            <div className={ui.stack}>
              <RiskBar score={tx.prediction.riskScore} threshold={tx.prediction.thresholdAtScoring} />

              <KeyValue rows={[
                ['Score', <span key="a" className="tnum" style={{ fontSize: '0.9375rem' }}>{tx.prediction.riskScore.toFixed(4)}</span>],
                ['Gate in force at scoring', <span key="b" className="tnum">{tx.prediction.thresholdAtScoring.toFixed(2)}</span>],
                ['Inference time', <span key="c" className="tnum">{tx.prediction.latencyMs} ms</span>],
                ['Scored at', absoluteTime(tx.prediction.predictedAt)],
              ]} />

              <hr className={ui.divider} />

              <div>
                <div className={s.sectionLabel}><Fingerprint size={12} /> Contributing features</div>
                <ContributionBars contributions={tx.prediction.topContributions} />
              </div>

              {tx.prediction.thresholdAtScoring !== settings.riskThreshold && (
                <Notice tone="warn">
                  The administrator has since changed the gate to {settings.riskThreshold.toFixed(2)}.
                  This record retains the value that was in force when the score was written, so the
                  decision stays interpretable.
                </Notice>
              )}
            </div>
          </Card>

          <Card
            title="Event trail"
            subtitle="Every state transition, on chain and off, in one chronological view (FR-07, FR-09)."
          >
            <AuditTimeline entries={entries} />
          </Card>
        </div>

        <div className={s.column}>
          <Card title="Risk at a glance">
            <RiskDial
              score={tx.prediction.riskScore}
              threshold={tx.prediction.thresholdAtScoring}
              size={150}
            />
          </Card>

          <Card title="Parties">
            <div>
              {parties.map((party) => {
                const isYou = party.user.id === principal.userId;
                return (
                  <div key={party.label} className={s.partyRow}>
                    <span className={`${s.partyAvatar} ${isYou ? s.partyAvatarYou : ''}`}>
                      {party.icon ?? initials(party.user.displayName)}
                    </span>
                    <div className={s.partyInfo}>
                      <div className={s.partyLabel}>{party.label}</div>
                      <div className={s.partyValue}>
                        {party.user.displayName}
                        {isYou && <span className={s.youTag}>You</span>}
                      </div>
                    </div>
                    <Address value={party.user.walletAddress} lead={5} tail={4} />
                  </div>
                );
              })}
            </div>
          </Card>

          <Card title="On-chain record" subtitle="Contracts this escrow transacted with.">
            <div className={ui.stack}>
              <div className={s.gasTotal}>
                <Fuel size={15} style={{ color: 'var(--chain)' }} />
                <span className={s.gasValue}>{gas(totalGas)}</span>
                <span className={s.gasLabel}>gas across {entries.filter((e) => e.gasUsed).length} calls</span>
              </div>

              <KeyValue rows={[
                ['EscrowContract', <Address key="a" value={contracts.escrow} lead={6} tail={4} />],
                ['RiskOracleContract', <Address key="b" value={contracts.riskOracle} lead={6} tail={4} />],
                ['Network', <Badge key="c" tone="chain">{contracts.network}</Badge>],
                ['Chain ID', <span key="d" className="mono">{contracts.chainId}</span>],
              ]} />

              {tx.chainStatus === 'confirmed' ? (
                <KeyValue rows={[
                  ['Escrow id', <span key="e" className="mono">{shortHash(tx.escrowIdHash)}</span>],
                  ['Oracle write', <TxLink key="f" hash={tx.scoreTxHash} />],
                  ['Deposit', <TxLink key="g" hash={tx.depositTxHash} />],
                  ...(tx.settlementTxHash
                    ? [['Settlement', <TxLink key="h" hash={tx.settlementTxHash} />] as [React.ReactNode, React.ReactNode]]
                    : []),
                ]} />
              ) : (
                <Notice tone="warn" icon={<Boxes size={15} />}>
                  This escrow has no on-chain record. Its identifier and any hashes shown are
                  local to this demonstration and must not be cited as verification.
                </Notice>
              )}
            </div>
          </Card>

          <Card title="Timeline">
            <KeyValue rows={[
              ['Initiated', absoluteTime(tx.createdAt)],
              ['Delivery confirmed', tx.deliveryConfirmedAt ? absoluteTime(tx.deliveryConfirmedAt) : '—'],
              ['Dispute raised', dispute ? absoluteTime(dispute.raisedAt) : '—'],
              ['Completed', tx.completedAt ? absoluteTime(tx.completedAt) : '—'],
              ['Holding period ends', absoluteTime(tx.holdingPeriodEndsAt)],
            ]} />
          </Card>
        </div>
      </div>
    </>
  );
}

/** A transaction hash, linked to Etherscan so a reader can check it. */
function TxLink({ hash }: { hash?: string }) {
  if (!hash) return <span className="mono">—</span>;
  return (
    <a
      className="mono"
      href={`https://sepolia.etherscan.io/tx/${hash}`}
      target="_blank"
      rel="noreferrer"
    >
      {shortHash(hash)}
    </a>
  );
}

/** Hashes are 66 characters; the ends are what a reader compares. */
function shortHash(hash?: string) {
  if (!hash) return '—';
  return hash.length > 18 ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : hash;
}
