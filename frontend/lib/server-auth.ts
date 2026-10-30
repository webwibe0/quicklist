import { cookies } from "next/headers";
import type { Account } from "./api";

export async function currentAccount(): Promise<Account | null> {
  const cookieStore = await cookies();
  if (!cookieStore.has("quicklist_session") && !cookieStore.has("__Host-quicklist_session")) return null;
  const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";
  const response = await fetch(`${backend}/api/v1/auth/me`, {
    headers: {Cookie: cookieStore.toString()}, cache: "no-store",
  });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Сервис аккаунтов временно недоступен.");
  return response.json();
}
