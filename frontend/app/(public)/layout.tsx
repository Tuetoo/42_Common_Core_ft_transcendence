import { SiteFooter } from "@/app/site-footer";

export default function PublicLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <main className="flex flex-1 flex-col items-center justify-center gap-10 p-4 sm:p-8">
        <div className="font-mono text-sm tracking-widest text-[var(--text-faint)] uppercase">AlphaMind</div>
        <div className="panel w-full max-w-sm">{children}</div>
      </main>
      <SiteFooter />
    </div>
  );
}
