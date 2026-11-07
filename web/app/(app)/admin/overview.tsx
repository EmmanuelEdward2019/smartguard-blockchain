'use client';

import Link from 'next/link';
import {
  Activity, Boxes, Cpu, Fuel, Gauge, KeyRound, Link2, Lock, Monitor,
  RotateCcw, ScrollText, ShieldAlert, Users,
} from 'lucide-react';
import {
  Address, Badge, Button, Card, KeyValue, Notice, Stat, StatGrid, ui,
} from '@/components/ui';
import { AuditTimeline } from '@/components/domain';
import { ROLE_LABEL, ROLE_PERMISSIONS } from '@/lib/auth/policy';
import { useStore } from '@/lib/store';
import { gas, pct } from '@/lib/format';
import { REPORTED_GAS, type Role } from '@/lib/types';
import a from './admin.module.css';

const LAYERS = [
  {
    icon: <Monitor size={15} />,
    name: 'Presentation layer',
    tech: 'React 19 · Next.js App Router',
    role: 'Authenticated buyer, seller, arbitrator and administrator workspaces.',
    tone: 'neutral' as const,
  },
  {
    icon: <Cpu size={15} />,
    name: 'Application & intelligence layer',
    tech: 'FastAPI · scikit-learn · PostgreSQL',
    role: 'Request authorisation, Random Forest inference, off-chain records.',
    tone: 'brand' as const,
  },
  {
    icon: <Link2 size={15} />,
    name: 'Oracle / integration layer',
    tech: 'Web3.py oracle bridge · event listener',
    role: 'Writes validated risk scores on chain; relays contract events back.',
    tone: 'chain' as const,
  },
  {
    icon: <Boxes size={15} />,
    name: 'Blockchain layer',
    tech: 'Solidity 0.8.20 · Ethereum Sepolia',
    role: 'EscrowContract and RiskOracleContract hold funds and enforce rules.',
    tone: 'chain' as const,
  },
];

const ROLES: Role[] = ['buyer', 'seller', 'arbitrator', 'admin'];

export function SystemOverview() {
  const { transactions, disputes, audit, settings, contracts, resetDemo } = useStore();

  const scored = transactions.length;
  const blockedShare = transactions.filter((t) => t.prediction.riskScore > settings.riskThreshold).length
    / Math.max(scored, 1);
  const meanLatency = Math.round(
    transactions.reduce((sum, t) => sum + t.prediction.latencyMs, 0) / Math.max(scored, 1),
  );
  const onChainCalls = audit.filter((e) => e.gasUsed).length;
  const totalGas = audit.reduce((sum, e) => sum + (e.gasUsed ?? 0), 0);

  return (
    <>
      <div className={ui.pageHead}>
        <div>
          <h1 className={ui.pageTitle}>System overview</h1>
          <p className={ui.pageSub}>
            The four architectural layers, the access model governing them, and the audit record
            that ties them together.
          </p>
        </div>
        <Button variant="ghost" onClick={resetDemo}>
          <RotateCcw size={15} /> Reset demonstration data
        </Button>
      </div>

      <div className={ui.stack}>
        <StatGrid>
          <Stat label="Transactions scored" value={scored} icon={<Gauge size={13} />} tone="brand"
            foot={`${pct(blockedShare, 1)} would be refused at the current gate`} />
          <Stat label="Mean inference time" value={meanLatency} unit="ms" icon={<Activity size={13} />}
            tone={meanLatency < 2000 ? 'safe' : 'blocked'} foot="NFR-01 budget is 2000 ms" />
          <Stat label="On-chain calls" value={onChainCalls} icon={<Boxes size={13} />} tone="chain"
            foot={`${gas(totalGas)} gas in total`} />
          <Stat label="Open disputes" value={disputes.filter((d) => !d.resolvedAt).length}
            icon={<ShieldAlert size={13} />}
            tone={disputes.some((d) => !d.resolvedAt) ? 'elevated' : 'safe'}
            foot={`${disputes.length} raised in total`} />
        </StatGrid>

        <div className={ui.split}>
          <Card title="Architecture" subtitle="One vocabulary throughout: four layers, three technical modules.">
            <div className={ui.stackSm}>
              {LAYERS.map((layer) => (
                <div key={layer.name} className={a.layerRow}>
                  <span
                    className={a.layerIcon}
                    style={{
                      color: layer.tone === 'brand' ? 'var(--brand)'
                        : layer.tone === 'chain' ? 'var(--chain)' : 'var(--text-dim)',
                    }}
                  >
                    {layer.icon}
                  </span>
                  <div>
                    <div className={a.layerHead}>
                      <span className={a.layerName}>{layer.name}</span>
                      <Badge tone={layer.tone}>{layer.tech}</Badge>
                    </div>
                    <div className={a.layerRole}>{layer.role}</div>
                  </div>
                </div>
              ))}
            </div>
          </Card>

          <div className={ui.stack}>
            <Card title="Deployment">
              <KeyValue rows={[
                ['Network', <Badge key="n" tone="chain">{contracts.network}</Badge>],
                ['Chain ID', <span key="c" className="mono">{contracts.chainId}</span>],
                ['EscrowContract', <Address key="e" value={contracts.escrow} lead={6} tail={4} />],
                ['RiskOracleContract', <Address key="r" value={contracts.riskOracle} lead={6} tail={4} />],
                ['Oracle operator', <Address key="o" value={contracts.oracleOperator} lead={6} tail={4} />],
              ]} />
            </Card>

            <Card title="Measured gas costs" subtitle="Per-operation medians from Sepolia. claimRefund is from the local harness — it sits behind a seven-day hold.">
              <div className={ui.stackSm}>
                {Object.entries(REPORTED_GAS).map(([fn, units]) => (
                  <div key={fn} className={a.gasRow}>
                    <Fuel size={12} style={{ color: 'var(--chain)', flex: 'none' }} />
                    <code className="mono" style={{ flex: 1 }}>{fn}</code>
                    <span className="tnum" style={{ color: 'var(--text)', fontWeight: 600 }}>{gas(units)}</span>
                  </div>
                ))}
              </div>
            </Card>
          </div>
        </div>

        <Card
          title="Access model"
          subtitle="Role grants are additive and no role inherits from another. Every grant is checked at the route, at the server component and, for anything touching funds, by the contract."
          flush
        >
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>Role</th>
                  <th className={ui.tdRight}>Grants</th>
                  <th>Permissions</th>
                </tr>
              </thead>
              <tbody>
                {ROLES.map((role) => (
                  <tr key={role}>
                    <td style={{ fontWeight: 600, color: 'var(--text)', whiteSpace: 'nowrap' }}>
                      <span className={ui.row} style={{ gap: 7 }}>
                        {role === 'admin' ? <KeyRound size={13} /> : <Users size={13} />}
                        {ROLE_LABEL[role]}
                      </span>
                    </td>
                    <td className={`${ui.tdRight} tnum`}>{ROLE_PERMISSIONS[role].length}</td>
                    <td>
                      <div className={a.permList}>
                        {ROLE_PERMISSIONS[role].map((permission) => (
                          <code key={permission} className={a.perm}>{permission}</code>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <Notice tone="info" icon={<Lock size={15} />} title="Where authorisation is decided">
          Sessions are signed server-side and carried in an HttpOnly cookie, so a role cannot be
          asserted by the browser. Route admission is an optimistic pre-filter; the authoritative
          checks run in the server component for each page and, once the backend exists, in the
          FastAPI dependency that guards every endpoint. Any action that moves funds is additionally
          restricted by an address modifier on the contract, which no client can circumvent.
        </Notice>

        <Card
          title="Audit log"
          subtitle="Every AI inference, oracle transmission and contract interaction (FR-09, NFR-06)."
          actions={
            <Link href="/admin/audit">
              <Button size="sm" variant="ghost"><ScrollText size={14} /> Full log</Button>
            </Link>
          }
        >
          <AuditTimeline entries={audit} limit={10} />
        </Card>
      </div>
    </>
  );
}
