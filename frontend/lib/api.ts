export type Account = {id: string; email: string; is_legacy_owner: boolean; email_verified?: boolean; access_allowed?: boolean};

export function csrfCookie(): string {
  if (typeof document === "undefined") return "";
  const parts = document.cookie.split("; ");
  const value = parts.find(item => item.startsWith("__Host-quicklist_csrf=")) || parts.find(item => item.startsWith("quicklist_csrf="));
  return value ? decodeURIComponent(value.slice(value.indexOf("=") + 1)) : "";
}

export async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD"].includes((init.method || "GET").toUpperCase()) && typeof document !== "undefined") {
    let token = csrfCookie();
    if (!token) {
      const response = await fetch("/api/v1/auth/csrf", {cache: "no-store", credentials: "same-origin", signal: init.signal});
      if (!response.ok) throw new Error("Не удалось проверить сессию. Обновите страницу.");
      token = (await response.json()).csrf_token;
    }
    headers.set("X-CSRF-Token", token);
  }
  const response = await fetch(url, {...init, headers, cache: "no-store", credentials: "same-origin"});
  if (response.status === 401 && typeof window !== "undefined" && !url.startsWith("/api/v1/auth/")) {
    window.dispatchEvent(new Event("quicklist-session-ended"));
  }
  return response;
}
