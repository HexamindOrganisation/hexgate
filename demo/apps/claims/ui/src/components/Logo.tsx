export function Logo({ size = 28, children }: { size?: number; children?: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 text-base font-bold tracking-tight">
      <img src="/static/logo.svg" width={size} height={size} alt="" />
      Claimdesk
      {children}
    </div>
  );
}
