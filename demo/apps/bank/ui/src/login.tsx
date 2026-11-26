import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import type { Account } from "@/lib/api";
import { postJson } from "@/lib/api";
import { Logo } from "@/components/Logo";
import { RolePill } from "@/components/RolePill";

const FEED = [
  { what: "MSG-3", where: "message from Sophie Bernard", when: "6 min ago" },
  { what: "MSG-2", where: "message from Marc Dubois", when: "40 min ago" },
];

function LoginPage() {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const pwRef = useRef<HTMLInputElement>(null);

  // The account list comes from users.yaml (no passwords).
  useEffect(() => {
    fetch("/api/accounts")
      .then((r) => r.json())
      .then(setAccounts)
      .catch(() => setFailed(true));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const r = await postJson("/api/login", { username: user, password: pw });
      if (r.ok) {
        location.href = "/";
        return;
      }
      setErr((await r.json()).detail || "Sign-in failed.");
    } catch {
      setErr("Tellerdesk is not reachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full flex-wrap">
      <section
        aria-label="About Tellerdesk"
        className="flex flex-[1_1_420px] flex-col gap-7 border-r border-line bg-panel p-12"
      >
        <Logo size={32} />
        <div className="grow" />
        <div className="flex flex-col gap-3.5">
          <h1 className="text-[34px]/[1.15] font-semibold tracking-tight">
            Serve customers faster. Ask the assistant.
          </h1>
          <p className="max-w-[460px] text-[15px] text-muted">
            Hexa Bank's branch back office: every customer file, the branch inbox, and an assistant
            that reads the request, looks up the accounts and moves the money for you.
          </p>
        </div>
        <div className="flex flex-col gap-2">
          {FEED.map((f) => (
            <div
              key={f.what}
              className="flex max-w-[460px] items-center gap-2.5 rounded-[10px] border border-line bg-card px-3 py-2.5 text-[13px]"
            >
              <span className="dot k-ok" />
              <span className="font-mono">{f.what}</span>
              <span className="sub">{f.where}</span>
              <span className="sub ml-auto">{f.when}</span>
            </div>
          ))}
        </div>
        <span className="sub">Hexa Bank · internal tool · demo data</span>
      </section>

      <main className="flex flex-[999_1_480px] items-center justify-center px-6 py-12">
        <div className="flex w-full max-w-[380px] flex-col gap-6">
          <div className="flex flex-col gap-1.5">
            <h2 className="text-2xl font-semibold">Sign in</h2>
            <span className="sub">Use your Hexa Bank account.</span>
          </div>
          <form className="flex flex-col gap-4" onSubmit={submit}>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="user" className="text-[13px] font-medium text-ink-2">
                Username
              </label>
              <input
                id="user"
                type="text"
                className="input"
                autoComplete="username"
                required
                autoFocus
                value={user}
                onChange={(e) => setUser(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="pw" className="text-[13px] font-medium text-ink-2">
                Password
              </label>
              <input
                id="pw"
                ref={pwRef}
                type="password"
                className="input"
                autoComplete="current-password"
                required
                value={pw}
                onChange={(e) => setPw(e.target.value)}
              />
            </div>
            <div id="err" role="alert" className="min-h-[18px] text-[13px] text-bad-ink">
              {err}
            </div>
            <button id="go" type="submit" className="btn" disabled={busy}>
              Sign in
            </button>
          </form>
          <details open className="box">
            <summary className="flex min-h-11 cursor-pointer items-center px-3.5 text-[13px] font-medium text-ink-2">
              Demo accounts (from users.yaml)
            </summary>
            <ul className="px-1.5 pb-2">
              {failed && <li className="sub p-2">Could not read users.yaml.</li>}
              {accounts?.map((a) => (
                <li key={a.username}>
                  <button
                    type="button"
                    title={`Use ${a.username}`}
                    className="flex min-h-10 w-full cursor-pointer items-center gap-2.5 rounded-lg px-2 py-1 text-left text-[13px] hover:bg-raised"
                    onClick={() => {
                      setUser(a.username);
                      pwRef.current?.focus();
                    }}
                  >
                    <span className="w-[70px] font-mono">{a.username}</span>
                    <span className="min-w-0 grow text-muted">{a.name}</span>
                    {a.roles.map((r) => (
                      <RolePill key={r} role={r} />
                    ))}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <LoginPage />
  </StrictMode>,
);
