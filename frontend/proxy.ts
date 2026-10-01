import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

import { isAuthorized, unauthorizedResponse } from "@/lib/auth";

/**
 * Basic-auth gate for the dashboard pages. `/api/*` is excluded on purpose: the API route handler
 * checks auth itself and streams large video uploads, which Proxy would buffer (10MB limit).
 */
export function proxy(request: NextRequest) {
  if (!isAuthorized(request.headers.get("authorization"))) {
    return unauthorizedResponse();
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|icon.svg).*)"],
};
