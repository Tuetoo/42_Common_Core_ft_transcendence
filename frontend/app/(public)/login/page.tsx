import { LoginForm } from "./login-form";
import { OAuthButtons } from "../oauth-buttons";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string }>;
}) {
  const { next } = await searchParams;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Sign in</p>
        <h1 className="page-title">Log in to AlphaMind</h1>
      </div>
      <LoginForm next={next ?? ""} />
      <OAuthButtons next={next ?? ""} />
    </div>
  );
}
