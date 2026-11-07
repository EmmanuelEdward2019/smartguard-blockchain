import { NextResponse } from 'next/server';
import { authenticate } from '@/lib/auth/accounts';
import { homeFor, type Principal } from '@/lib/auth/policy';
import { SESSION_COOKIE, SESSION_TTL_SECONDS, sessionCookieOptions, signSession } from '@/lib/auth/session';
import { API_PREFIX, API_URL } from '@/lib/server/api';

interface LoginBody {
  email?: unknown;
  password?: unknown;
}

interface ApiLogin {
  accessToken?: string;
  principal?: Principal;
}

/**
 * Asks the API to authenticate, and returns its token with the principal.
 *
 * Returns null when the API cannot be reached or rejects the credentials, so
 * the caller can fall back to the local account directory. A prototype that
 * refuses to sign anybody in because a backend is down is less useful than one
 * that says which mode it is running in.
 */
async function authenticateAgainstApi(
  email: string, password: string,
): Promise<{ principal: Principal; apiToken: string } | null> {
  try {
    const response = await fetch(`${API_URL}${API_PREFIX}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      cache: 'no-store',
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) return null;
    const body = (await response.json()) as ApiLogin;
    if (!body.accessToken || !body.principal) return null;
    return { principal: body.principal, apiToken: body.accessToken };
  } catch {
    return null;
  }
}

/**
 * Establishes a session.
 *
 * The credentials go to `POST /api/v1/auth/login` first. Its token is kept
 * inside the HttpOnly cookie, never handed to the browser, and the route
 * handlers under app/api forward it. If the API is unreachable the local
 * account directory still authenticates, the session carries no API token,
 * and the interface works from its local engine — which it labels as such.
 */
export async function POST(request: Request) {
  let body: LoginBody;
  try {
    body = (await request.json()) as LoginBody;
  } catch {
    return NextResponse.json({ error: 'Malformed request body.' }, { status: 400 });
  }

  const email = typeof body.email === 'string' ? body.email : '';
  const password = typeof body.password === 'string' ? body.password : '';

  if (!email || !password) {
    return NextResponse.json({ error: 'Email address and password are required.' }, { status: 400 });
  }

  const fromApi = await authenticateAgainstApi(email, password);

  let principal: Principal;
  let apiToken: string | undefined;

  if (fromApi) {
    principal = fromApi.principal;
    apiToken = fromApi.apiToken;
  } else {
    const result = await authenticate(email, password);
    if (!result.ok) {
      // One message for both failure modes, so the response does not confirm
      // whether an account exists.
      return NextResponse.json(
        { error: 'Those credentials were not recognised.' }, { status: 401 });
    }
    principal = result.principal;
  }

  const token = await signSession(principal, apiToken);
  const response = NextResponse.json({
    principal,
    backend: apiToken ? 'api' : 'local',
    redirectTo: homeFor(principal.role),
  });
  response.cookies.set(SESSION_COOKIE, token, sessionCookieOptions(SESSION_TTL_SECONDS));
  return response;
}
