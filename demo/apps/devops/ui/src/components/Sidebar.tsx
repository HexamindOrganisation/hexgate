import { Activity, AlignLeft, LayoutGrid, LogOut, Settings } from "lucide-react";

import type { Me } from "@/lib/api";
import { postJson } from "@/lib/api";
import { initials } from "@/lib/utils";
import { Logo } from "@/components/Logo";
import { RolePill } from "@/components/RolePill";

const NAV = [
  { label: "Environments", Icon: LayoutGrid, active: true },
  { label: "Deploys", Icon: AlignLeft },
  { label: "Incidents", Icon: Activity, incidents: true },
  { label: "Settings", Icon: Settings },
];

export function Sidebar({ me, incidents }: { me: Me | null; incidents: number }) {
  async function logout() {
    await postJson("/api/logout");
    location.href = "/login";
  }

  return (
    <nav
      aria-label="Main"
      className="flex max-w-[280px] flex-[1_1_232px] flex-col gap-6 border-r border-line bg-panel px-3.5 py-5"
    >
      <Logo>
        <span className="ml-auto rounded-md border border-line-strong px-1.5 py-0.5 text-[11px] font-normal text-faint">
          Acme Inc.
        </span>
      </Logo>
      <div className="flex flex-col gap-0.5">
        {NAV.map(({ label, Icon, active, incidents: count }) => (
          <a key={label} href="#" className={active ? "nav-link active" : "nav-link"}>
            <Icon size={18} strokeWidth={1.8} aria-hidden />
            {label}
            {count && incidents > 0 && (
              <span
                id="incidents"
                className="ml-auto rounded-full bg-warn px-[7px] py-px text-[11px] font-semibold text-white"
              >
                {incidents}
              </span>
            )}
          </a>
        ))}
      </div>
      <div className="grow" />
      <div className="flex items-center gap-2.5 rounded-[10px] border border-line bg-card p-2.5">
        <div
          aria-hidden
          className="flex size-9 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[13px] font-semibold text-accent-deep"
        >
          {me ? initials(me.name) : ""}
        </div>
        <div className="flex min-w-0 grow flex-col gap-[3px]">
          <b className="truncate text-[13px]">{me?.name}</b>
          <span className="flex flex-wrap gap-1">
            {(me?.roles.length ? me.roles : ["no role"]).map((r) => (
              <RolePill key={r} role={r} />
            ))}
          </span>
        </div>
        <button
          id="logout"
          type="button"
          className="icon-btn"
          aria-label="Sign out"
          title="Sign out"
          onClick={logout}
        >
          <LogOut size={18} strokeWidth={1.8} aria-hidden />
        </button>
      </div>
    </nav>
  );
}
