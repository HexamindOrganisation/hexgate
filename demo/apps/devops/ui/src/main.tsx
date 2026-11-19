import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import type { AppState, Me } from "@/lib/api";
import { getJson } from "@/lib/api";
import { Copilot } from "@/components/Copilot";
import { Environments, health } from "@/components/Environments";
import { Sidebar } from "@/components/Sidebar";

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<AppState | null>(null);

  useEffect(() => {
    void getJson<Me>("/api/me").then(setMe);
  }, []);

  // The environments, activity and badge follow the server: poll every second.
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

  const services = state?.services ?? [];
  const incidents = services.reduce(
    (n, s) =>
      n + Object.values(s.envs).filter((d) => ["critical", "degraded"].includes(health(d))).length,
    0,
  );

  return (
    <div className="flex min-h-full flex-wrap items-stretch">
      <Sidebar me={me} incidents={incidents} />
      <div className="flex min-w-0 flex-[999_1_560px] flex-wrap items-stretch">
        <Environments services={services} events={state?.events ?? []} />
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
