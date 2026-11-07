/**
 * Escrow collection — `GET /api/escrows`, `POST /api/escrows`.
 *
 * A same-origin shim over the API. It adds no logic of its own: authorisation
 * is decided by FastAPI, which checks the bearer token this handler attaches,
 * and the status it returns is passed through unchanged. A 422 from the risk
 * gate has to reach the interface as a 422, because "your escrow was refused
 * by the risk check" and "something went wrong" are different messages.
 */

import { NextResponse } from 'next/server';
import { callApi } from '@/lib/server/api';

export const dynamic = 'force-dynamic';

export async function GET() {
  const result = await callApi('/escrows');
  return NextResponse.json(
    result.ok ? result.data : { error: result.error },
    { status: result.status },
  );
}

export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Malformed request body.' }, { status: 400 });
  }

  const result = await callApi('/escrows', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return NextResponse.json(
    result.ok ? result.data : { error: result.error },
    { status: result.status },
  );
}
