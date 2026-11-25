export function Logo({ size = 28 }: { size?: number }) {
  return (
    <div className="flex items-center gap-2.5 text-base font-bold tracking-tight">
      <img src="/static/logo.svg" width={size} height={size} alt="" />
      Tellerdesk
    </div>
  );
}
