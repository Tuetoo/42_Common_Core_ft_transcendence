import { LoginForm } from "./login-form";
import { OAuthButtons } from "../oauth-buttons";
import { firstParam, type SearchParam } from "@/app/lib/format";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: SearchParam }>;
}) {
  const next = firstParam((await searchParams).next);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Sign in</p>
        <h1 className="page-title">Log in to AlphaMind</h1>
      </div>
      <LoginForm next={next} />
      <OAuthButtons next={next} />
    </div>
  );
}
