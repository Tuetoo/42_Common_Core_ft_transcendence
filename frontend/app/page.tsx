import { redirect } from "next/navigation";
import { isLoggedIn } from "@/app/lib/session";

export default async function RootPage() {
  redirect((await isLoggedIn()) ? "/portfolios" : "/login");
}
