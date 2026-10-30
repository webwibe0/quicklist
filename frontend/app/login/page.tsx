import { redirect } from "next/navigation";
import { AuthForm } from "@/components/auth-form";
import { currentAccount } from "@/lib/server-auth";
export const dynamic = "force-dynamic";
export default async function Login() {
  if (await currentAccount()) redirect("/");
  return <AuthForm mode="login" />;
}
