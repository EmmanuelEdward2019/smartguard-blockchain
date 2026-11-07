/**
 * Lifecycle actions — `POST /api/escrows/{reference}/{action}`.
 *
 * The action is matched against a fixed set rather than forwarded as given.
 * Without that, this handler would proxy any path segment a caller invented
 * onto the API, which is a request-forgery surface for no benefit — the five
 * transitions the contract has are known in advance.
 */

import { NextResponse } from 'next/server';
import { callApi } from '@/lib/server/api';

export const dynamic = 'force-dynamic';

const ACTIONS = new Set([
  'confirm-delivery',
  'withdraw',
  'dispute',
  'resolve',
  'refund',
]);

export async function POST(
  request: Request,
  { params }: { params: Promise<{ reference: string; action: string }> },
) {
  const { reference, action } = await params;

  if (!ACTIONS.has(action)) {
    return NextResponse.json({ error: 'No such action.' }, { status: 404 });
  }

  // `confirm-delivery`, `withdraw` and `refund` carry no body; `dispute` and
  // `resolve` do. An absent body is not an error.
  let body: unknown = undefined;
  try {
    const text = await request.text();
    if (text) body = JSON.parse(text);
  } catch {
    return NextResponse.json({ error: 'Malformed request body.' }, { status: 400 });
  }

  const result = await callApi(
    `/escrows/${encodeURIComponent(reference)}/${action}`,
    { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) },
  );
  return NextResponse.json(
    result.ok ? result.data : { error: result.error },
    { status: result.status },
  );
}
