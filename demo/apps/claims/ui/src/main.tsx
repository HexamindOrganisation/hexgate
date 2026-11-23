import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import type { AppState, Me } from "@/lib/api";
import { getJson } from "@/lib/api";
import { Claims } from "@/components/Claims";
import { Copilot } from "@/components/Copilot";
import { Sidebar } from "@/components/Sidebar";

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<AppState | null>(null);

  useEffect(() => {
    void getJson<Me>("/api/me").then(setMe);
  }, []);

  // The claims, activity and badge follow the server: poll every second.
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        setState(await getJson<AppState>("/api/state"));
      } catch {
        // server restarting (agent.py reload): try again
      }
      if (!stop) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, []);

  const claims = state?.claims ?? [];
  const flagged = claims.filter((c) => c.flags.length > 0).length;

  return (
    <div className="flex min-h-full flex-wrap items-stretch">
      <Sidebar me={me} flagged={flagged} />
      <div className="flex min-w-0 flex-[999_1_560px] flex-wrap items-stretch">
        <Claims
          claims={claims}
          events={state?.events ?? []}
          paidTotal={state?.paid_total_eur ?? 0}
        />
        <Copilot me={me} status={state ?? me} />
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
