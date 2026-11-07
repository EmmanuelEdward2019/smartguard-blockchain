/**
 * Session tokens.
 *
 * A compact JWS (HS256) carrying the principal, signed with a server-held
 * secret and delivered in an HttpOnly, SameSite=Lax cookie. The browser can
 * neither read nor forge it, which is the difference between a session and a
 * client-side role flag.
 *
 * Implemented with Web Crypto so the same code runs in the Node runtime (route
 * handlers, server components) and the Edge runtime (the proxy).
 *
 * When the FastAPI service takes over authentication it will issue the token
 * instead; this module then verifies its signature against the shared secret
 * and nothing else in the application changes.
 */

import type { Principal } from './policy';
import type { Role } from '@/lib/types';

export const SESSION_COOKIE = 'sg_session';
export const SESSION_TTL_SECONDS = 60 * 60 * 8; // one working day

interface Claims extends Principal {
  /** Issued at, seconds since epoch. */
  iat: number;
  /** Expiry, seconds since epoch. */
  exp: number;
  /**
   * The bearer token the FastAPI service issued for this principal.
   *
   * Carried inside the session rather than handed to the browser: the API
   * authenticates with a bearer token, the browser holds an HttpOnly cookie,
   * and the route handlers in app/api bridge the two server-side. A token in
   * JavaScript's reach would undo the reason the cookie is HttpOnly.
   *
   * Absent when the API is not configured, in which case the interface falls
   * back to its local engine and says so.
   */
  apiToken?: string;
}

/* ----------------------------------------------------------------- key --- */

function secretBytes(): Uint8Array {
  const secret = process.env.SESSION_SECRET;
  if (!secret) {
    // A fixed development fallback keeps local runs working. Production must
    // set SESSION_SECRET; sessions signed with this key are not trustworthy.
    if (process.env.NODE_ENV === 'production') {
      throw new Error(
        'SESSION_SECRET is not set. Refusing to sign sessions with the development key.',
      );
    }
    return new TextEncoder().encode('smartguard-development-key-do-not-use-in-production');
  }
  return new TextEncoder().encode(secret);
}

let keyPromise: Promise<CryptoKey> | null = null;

function hmacKey(): Promise<CryptoKey> {
  if (!keyPromise) {
    keyPromise = crypto.subtle.importKey(
      'raw',
      secretBytes() as BufferSource,
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign', 'verify'],
    );
  }
  return keyPromise;
}

/* ------------------------------------------------------------- base64 --- */

function b64urlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(value: string): Uint8Array {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(Math.ceil(value.length / 4) * 4, '=');
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const encodeJson = (value: unknown) =>
  b64urlEncode(new TextEncoder().encode(JSON.stringify(value)));

/* -------------------------------------------------------------- issue --- */

export async function signSession(
  principal: Principal,
  apiToken?: string,
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const claims: Claims = {
    ...principal, iat: now, exp: now + SESSION_TTL_SECONDS,
    ...(apiToken ? { apiToken } : {}),
  };

  const header = encodeJson({ alg: 'HS256', typ: 'JWT' });
  const payload = encodeJson(claims);
  const signingInput = `${header}.${payload}`;

  const signature = await crypto.subtle.sign(
    'HMAC',
    await hmacKey(),
    new TextEncoder().encode(signingInput) as BufferSource,
  );

  return `${signingInput}.${b64urlEncode(new Uint8Array(signature))}`;
}

/* ------------------------------------------------------------- verify --- */

const VALID_ROLES: Role[] = ['buyer', 'seller', 'arbitrator', 'admin'];

/**
 * Returns the principal, or null for anything that is not a currently valid
 * token. Never throws: a malformed cookie is an anonymous request, not a
 * server error.
 */
export async function verifySession(token: string | undefined): Promise<Principal | null> {
  if (!token) return null;

  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [header, payload, signature] = parts;

  let valid: boolean;
  try {
    valid = await crypto.subtle.verify(
      'HMAC',
      await hmacKey(),
      b64urlDecode(signature) as BufferSource,
      new TextEncoder().encode(`${header}.${payload}`) as BufferSource,
    );
  } catch {
    return null;
  }
  if (!valid) return null;

  let claims: Claims;
  try {
    claims = JSON.parse(new TextDecoder().decode(b64urlDecode(payload))) as Claims;
  } catch {
    return null;
  }

  if (typeof claims.exp !== 'number' || claims.exp <= Math.floor(Date.now() / 1000)) return null;
  if (!claims.userId || !claims.walletAddress) return null;
  if (!VALID_ROLES.includes(claims.role)) return null;

  return {
    userId: claims.userId,
    walletAddress: claims.walletAddress,
    role: claims.role,
    displayName: claims.displayName,
  };
}

/**
 * The API bearer token out of a session cookie, for server-side forwarding.
 *
 * Deliberately separate from `verifySession`, which returns the public
 * `Principal` and is called from the proxy and from server components. Only
 * the route handlers that forward to FastAPI need the token, and keeping it
 * off the principal means it cannot be serialised into a page by accident.
 */
export async function readApiToken(token: string | undefined): Promise<string | null> {
  if (!token) return null;
  // Signature and expiry are checked here too; an unverified cookie must not
  // yield a token to forward.
  if (!(await verifySession(token))) return null;
  try {
    const payload = token.split('.')[1];
    const claims = JSON.parse(new TextDecoder().decode(b64urlDecode(payload))) as Claims;
    return claims.apiToken ?? null;
  } catch {
    return null;
  }
}

/** Cookie attributes used by both the login and logout handlers. */
export function sessionCookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    sameSite: 'lax' as const,
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge,
  };
}
