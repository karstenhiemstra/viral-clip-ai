/**
 * Optional HTTP Basic auth for the whole dashboard (pages + API proxy).
 * Enabled when DASHBOARD_PASSWORD is set; the user name defaults to "admin".
 */
export function basicAuthEnabled(): boolean {
  return Boolean(process.env.DASHBOARD_PASSWORD);
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function isAuthorized(authorization: string | null): boolean {
  if (!basicAuthEnabled()) return true;
  if (!authorization?.startsWith("Basic ")) return false;
  let decoded = "";
  try {
    decoded = atob(authorization.slice(6).trim());
  } catch {
    return false;
  }
  const sep = decoded.indexOf(":");
  const user = decoded.slice(0, sep);
  const pass = decoded.slice(sep + 1);
  const expectedUser = process.env.DASHBOARD_USER || "admin";
  return safeEqual(user, expectedUser) && safeEqual(pass, process.env.DASHBOARD_PASSWORD || "");
}

export function unauthorizedResponse(): Response {
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="ViralClip AI", charset="UTF-8"' },
  });
}
