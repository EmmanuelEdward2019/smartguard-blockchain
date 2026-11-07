'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  ChevronRight, FlaskConical, Gavel, LogOut, Menu, Moon, PlusCircle,
  ScrollText, Settings2, Shield, ShieldCheck, Sun, Wallet, X,
} from 'lucide-react';
import { useSession } from '@/lib/auth/context';
import {
  ROLE_DESCRIPTION, ROLE_LABEL, ROLE_PERMISSIONS, visibleDisputes, visibleEscrows,
  type Permission,
} from '@/lib/auth/policy';
import { useStore } from '@/lib/store';
import { useTheme } from '@/lib/theme';
import { shortAddress } from '@/lib/format';
import s from './app-shell.module.css';

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
  /** Rendered only when the principal holds this permission. */
  permission: Permission;
  count?: number;
  alert?: boolean;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [theme, toggleTheme] = useTheme();
  // The menu closes on navigation. Storing the path it was opened on derives
  // that from the current route instead of writing state in an effect.
  const [menu, setMenu] = useState({ open: false, at: '' });
  const menuOpen = menu.open && menu.at === pathname;
  const setMenuOpen = (open: boolean) => setMenu({ open, at: pathname });
  const { principal, can, signOut } = useSession();
  const { transactions, disputes, contracts, backendConnected } = useStore();

  const mine = useMemo(
    () => visibleEscrows(principal, transactions),
    [principal, transactions],
  );
  const myDisputes = useMemo(
    () => visibleDisputes(principal, disputes, transactions),
    [principal, disputes, transactions],
  );

  const openEscrows = mine.filter((t) => t.state !== 'COMPLETED').length;
  const openDisputes = myDisputes.filter((d) => !d.resolvedAt).length;

  // The navigation is derived from the principal's permissions, not from a
  // role switch. A link the role cannot use is not rendered — and the route
  // behind it would reject the request even if it were.
  const items: NavItem[] = [
    { href: '/escrows',      label: 'Escrows',          icon: <Wallet size={16} />,     permission: 'escrow:read:own', count: openEscrows },
    { href: '/escrows/new',  label: 'New escrow',       icon: <PlusCircle size={16} />, permission: 'escrow:create' },
    { href: '/disputes',     label: 'Dispute queue',    icon: <Gavel size={16} />,      permission: 'dispute:read:any', count: openDisputes, alert: openDisputes > 0 },
    { href: '/admin',        label: 'System overview',  icon: <Shield size={16} />,     permission: 'admin:settings:read' },
    { href: '/admin/model',  label: 'Model & features', icon: <FlaskConical size={16} />, permission: 'admin:model:read' },
    { href: '/admin/settings', label: 'Risk configuration', icon: <Settings2 size={16} />, permission: 'admin:settings:write' },
    { href: '/admin/audit',  label: 'Audit log',        icon: <ScrollText size={16} />, permission: 'admin:audit:read' },
  ];

  // `escrow:read:any` holders (arbitrator, administrator) also need the list.
  const visibleItems = items.filter(
    (item) => can(item.permission) || (item.href === '/escrows' && can('escrow:read:any')),
  );

  const active = visibleItems
    .filter((i) => pathname === i.href || pathname.startsWith(`${i.href}/`))
    .sort((a, b) => b.href.length - a.href.length)[0];

  const initials = principal.displayName.split(' ').map((w) => w[0]).slice(0, 2).join('');
  const grants = ROLE_PERMISSIONS[principal.role];

  return (
    <div className={s.shell}>
      {menuOpen && (
        <button className={s.scrim} onClick={() => setMenuOpen(false)} aria-label="Close navigation" />
      )}

      <aside className={`${s.sidebar} ${menuOpen ? s.sidebarOpen : ''}`}>
        <div className={s.brand}>
          <span className={s.brandMark}><Shield size={17} /></span>
          <div>
            <div className={s.brandName}>SmartGuard</div>
            <div className={s.brandNet}>
              <span className={s.netDot} />
              {contracts.network}
            </div>
          </div>
        </div>

        <nav className={s.nav} aria-label="Main">
          <div className={s.navGroup}>Workspace</div>
          {visibleItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`${s.navItem} ${active?.href === item.href ? s.navItemActive : ''}`}
              aria-current={active?.href === item.href ? 'page' : undefined}
            >
              <span className={s.navIcon}>{item.icon}</span>
              {item.label}
              {item.count ? (
                <span className={`${s.navCount} ${item.alert ? s.navCountAlert : ''}`}>{item.count}</span>
              ) : null}
            </Link>
          ))}
        </nav>

        <div className={s.roleCard}>
          <div className={s.roleHead}>
            <ShieldCheck size={13} style={{ color: 'var(--brand)' }} />
            {ROLE_LABEL[principal.role]}
          </div>
          <p className={s.roleBody}>{ROLE_DESCRIPTION[principal.role]}</p>
          <div className={s.rolePerms}>
            {grants.slice(0, 4).map((permission) => (
              <span key={permission} className={s.perm}>{permission}</span>
            ))}
            {grants.length > 4 && <span className={s.perm}>+{grants.length - 4}</span>}
          </div>
        </div>

        <div className={s.sidebarFoot}>
          <Link href="/" className={s.navItem}>
            <span className={s.navIcon}><Shield size={16} /></span>
            About this system
          </Link>
          <button className={s.signOut} onClick={signOut}>
            <span className={s.navIcon}><LogOut size={16} /></span>
            Sign out
          </button>
        </div>
      </aside>

      <div className={s.main}>
        <header className={s.header}>
          <button
            className={`${s.iconBtn} ${s.menuBtn}`}
            onClick={() => setMenuOpen(!menuOpen)}
            aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
          >
            {menuOpen ? <X size={16} /> : <Menu size={16} />}
          </button>

          <div className={s.crumbs}>
            <span>{ROLE_LABEL[principal.role]}</span>
            <ChevronRight size={13} />
            <span className={s.crumbCurrent}>{active?.label ?? 'Detail'}</span>
          </div>

          <div className={s.headerActions}>
            <button
              className={s.iconBtn}
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
            >
              {theme === 'light' ? <Moon size={15} /> : <Sun size={15} />}
            </button>

            <div className={s.identity}>
              <span className={s.avatar}>{initials}</span>
              <div>
                <div className={s.identityName}>{principal.displayName}</div>
                <div className={s.identityRole}>{shortAddress(principal.walletAddress)}</div>
              </div>
            </div>
          </div>
        </header>

        {!backendConnected && (
          <div className={s.demoBar}>
            <FlaskConical size={13} />
            <span>
              <span className={s.demoBarStrong}>Offline mode.</span>{' '}
              The API is not reachable, so figures on this page come from a local stand-in rather
              than from the model and the contract.
            </span>
          </div>
        )}

        <main className={s.content}>{children}</main>
      </div>
    </div>
  );
}
