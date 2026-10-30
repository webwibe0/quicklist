import { redirect } from "next/navigation";
import QuickListApp from "@/components/quicklist-app";
import { PublicHome } from "@/components/public-pages";
import { currentAccount } from "@/lib/server-auth";
export const dynamic = "force-dynamic";
export default async function Home() {
  const account = await currentAccount();
  if (!account) return <PublicHome />;
  if (account.access_allowed === false) redirect('/verify-email');
  return <QuickListApp account={account} />;
}
