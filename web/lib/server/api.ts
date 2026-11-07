/**
 * The server-side seam between the presentation layer and the API.
 *
 * The browser never talks to FastAPI directly. It holds an HttpOnly cookie and
 * calls this application's own route handlers; those handlers read the bearer
 * token out of that cookie and forward the request. Two reasons, and the
 * second is the one that matters.
 *
 * A bearer token reachable from JavaScript would undo the point of an HttpOnly
 * cookie — anything that can read it can replay it. And a browser calling a
 * different origin needs CORS opened wide enough to be worth attacking, where
 * a same-origin call needs none at all.
 *
 * `API_URL` is a server-only variable on purpose. `NEXT_PUBLIC_API_URL` is
 * compiled into the client bundle and is read by lib/api.ts to decide whether
 * to use the local scoring stand-in; this one is never shipped to a browser.
 */

import { cookies } from 'next/headers';
import { SESSION_COOKIE, readApiToken } from '@/lib/auth/session';

export const API_URL = process.env.API_URL ?? 'http://127.0.0.1:8000';
export const API_PREFIX = '/api/v1';

/** How long to wait on the API before giving up. */
const TIMEOUT_MS = 30_000;

export class ApiProxyError extends Error {
  constructor(message: string, readonly status: number, readonly body?: unknown) {
    super(message);
    this.name = 'ApiProxyError';
  }
}

export interface ApiResult<T> {
  ok: boolean;
  status: number;
  data: T | null;
  error?: string;
}

async function bearer(): Promise<string | null> {
  const jar = await cookies();
  return readApiToken(jar.get(SESSION_COOKIE)?.value);
}

/**
 * Forwards one request to the API as the signed-in principal.
 *
 * Returns the status rather than throwing on a 4xx: the API's refusals are
 * meaningful — 403 for a permission, 422 for a rejected risk score — and a
 * route handler should pass them through rather than flatten them into 500.
 */
export async function callApi<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<ApiResult<T>> {
  const token = await bearer();
  if (!token) {
    return { ok: false, status: 401, data: null, error: 'Not signed in.' };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const response = await fetch(`${API_URL}${API_PREFIX}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        ...(init.headers ?? {}),
      },
      signal: controller.signal,
      cache: 'no-store',
    });

    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch { parsed = text; }
    }

    if (!response.ok) {
      const detail =
        typeof parsed === 'object' && parsed !== null && 'detail' in parsed
          ? (parsed as { detail: unknown }).detail
          : parsed;
      return {
        ok: false,
        status: response.status,
        data: null,
        error: typeof detail === 'string' ? detail : JSON.stringify(detail ?? 'Request failed.'),
      };
    }

    return { ok: true, status: response.status, data: parsed as T };
  } catch (error) {
    // A timeout or a refused connection is not the caller's fault, and the
    // interface should say the backend is unreachable rather than show an
    // empty list as though there were nothing to show.
    const reason =
      error instanceof Error && error.name === 'AbortError'
        ? `The API did not respond within ${TIMEOUT_MS / 1000}s.`
        : `Could not reach the API at ${API_URL}.`;
    return { ok: false, status: 503, data: null, error: reason };
  } finally {
    clearTimeout(timer);
  }
}

/** Whether a server-side API is configured at all. */
export const isApiConfigured = () => Boolean(API_URL);
