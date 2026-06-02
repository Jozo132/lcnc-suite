/**
 * Auth token plumbing (issue #17).
 *
 * The gateway injects `window.__LCNC_TOKEN__` into the served index.html. In
 * dev (page served by Vite) it falls back to the build-time `VITE_WEBUI_TOKEN`.
 * An empty token means auth is disabled (loopback/dev) — every gated surface
 * treats that as "send nothing", and the backend `require_token` is a no-op.
 */

declare global {
  interface Window {
    __LCNC_TOKEN__?: string;
  }
}

export const authToken: string =
  (typeof window !== "undefined" && window.__LCNC_TOKEN__) ||
  ((import.meta as any).env?.VITE_WEBUI_TOKEN as string | undefined) ||
  "";

/** Headers to merge into REST mutation fetches. */
export function authHeaders(): Record<string, string> {
  return authToken ? { "X-Auth-Token": authToken } : {};
}

/**
 * Append `?token=` to a URL. Needed for the WebSocket (browsers can't set WS
 * headers) and for `navigator.sendBeacon` (can't set headers either).
 */
export function withToken(url: string): string {
  if (!authToken) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(authToken);
}
