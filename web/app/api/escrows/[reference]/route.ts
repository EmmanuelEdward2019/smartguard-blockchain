/** One escrow — `GET /api/escrows/{reference}`. */

import { NextResponse } from 'next/server';
import { callApi } from '@/lib/server/api';

export const dynamic = 'force-dynamic';

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ reference: string }> },
) {
  const { reference } = await params;
  const result = await callApi(`/escrows/${encodeURIComponent(reference)}`);
  return NextResponse.json(
    result.ok ? result.data : { error: result.error },
    { status: result.status },
  );
}
