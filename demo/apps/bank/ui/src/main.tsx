import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import type { AppState, Me } from "@/lib/api";
import { getJson, postJson } from "@/lib/api";
import { normalizeIban } from "@/lib/utils";
import { Copilot } from "@/components/Copilot";
import { CustomerFile } from "@/components/CustomerFile";
import { Sidebar } from "@/components/Sidebar";

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<AppState | null>(null);

  useEffect(() => {
    void getJson<Me>("/api/me").then(setMe);
  }, []);

  // The accounts, inbox, activity and badge follow the server: poll every second.
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

  async function openFile(customerId: string) {
    await postJson("/api/session", { customer_id: customerId });
    setState(await getJson<AppState>("/api/state"));
  }

  const customers = state?.customers ?? [];
  const openId = state?.open_customer ?? me?.open_customer ?? "";
  const open = customers.find((c) => c.id === openId);
  const beneficiaries = (open?.beneficiaries ?? []).map((b) => normalizeIban(b.iban));
  const flagged = customers
    .flatMap((c) => c.accounts)
    .filter((a) => a.flags.length > 0 || a.closed).length;

  return (
    <div className="flex min-h-full flex-wrap items-stretch">
      <Sidebar me={me} flagged={flagged} />
      <div className="flex min-w-0 flex-[999_1_560px] flex-wrap items-stretch">
        <CustomerFile
          customers={customers}
          openId={openId}
          onOpen={openFile}
          inbox={state?.inbox ?? []}
          events={state?.events ?? []}
          movedOut={state?.moved_out_eur ?? 0}
        />
        <Copilot me={me} status={state ?? me} customerId={openId} beneficiaries={beneficiaries} />
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
