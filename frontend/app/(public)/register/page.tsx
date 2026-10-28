import { RegisterForm } from "./register-form";
import { OAuthButtons } from "../oauth-buttons";
import { firstParam, type SearchParam } from "@/app/lib/format";

export default async function RegisterPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: SearchParam }>;
}) {
  const next = firstParam((await searchParams).next);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Get started</p>
        <h1 className="page-title">Create your AlphaMind account</h1>
      </div>
      <RegisterForm next={next} />
      <OAuthButtons next={next} />
    </div>
  );
}
