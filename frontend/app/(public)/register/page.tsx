import { RegisterForm } from "./register-form";
import { OAuthButtons } from "../oauth-buttons";

export default function RegisterPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <p className="page-eyebrow">Get started</p>
        <h1 className="page-title">Create your AlphaMind account</h1>
      </div>
      <RegisterForm />
      <OAuthButtons />
    </div>
  );
}
