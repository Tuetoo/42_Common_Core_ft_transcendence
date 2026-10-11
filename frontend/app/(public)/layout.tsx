export default function PublicLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-10 p-8">
      <div style={{ fontFamily: "var(--font-plex-mono)" }} className="text-sm tracking-widest text-[var(--text-faint)] uppercase">
        AlphaMind
      </div>
      <div className="panel w-full max-w-sm">{children}</div>
    </div>
  );
}
