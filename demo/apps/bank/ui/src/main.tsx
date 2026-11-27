import { StrictMode, useEffect, useRef, useState } from "react";
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
  const [busy, setBusy] = useState(false);
  const stateSeq = useRef(0);

  // Only the latest fetch may write: a poll sent before a file switch can land after it.
  async function refreshState() {
    const seq = ++stateSeq.current;
    const next = await getJson<AppState>("/api/state");
    if (seq === stateSeq.current) setState(next);
  }

  useEffect(() => {
    void getJson<Me>("/api/me").then(setMe);
  }, []);

  // The accounts, inbox, activity and badge follow the server: poll every second.
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await refreshState();
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
    await refreshState();
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
          locked={busy}
          inbox={state?.inbox ?? []}
          events={state?.events ?? []}
          movedOut={state?.moved_out_eur ?? 0}
        />
        <Copilot
          me={me}
          status={state ?? me}
          customerId={openId}
          beneficiaries={beneficiaries}
          busy={busy}
          onBusyChange={setBusy}
        />
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
