/**
 * The quickstart dialog: the platform half of docs/quickstart.mdx as six short
 * steps, each with the copy-paste code on one side and an animated sketch of
 * what happens on the other. Opened from the sidebar and Home's "Get started"
 * checklist, and once unprompted by Home for a project with no agents.
 * Open/step/seen state lives in the `useUi` store.
 */

import { useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  Copy,
  FolderPlus,
  RotateCcw,
} from "lucide-react";

import { CreateProjectDialog } from "@/components/CreateProjectDialog";

import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ENV_FILE,
  QUICKSTART_STEP_IDS,
  type QuickstartStepId,
  FRAMEWORKS,
  INSTALL,
  POLICY_YAML,
  REGISTER,
  RUN_COMMAND,
  RUN_OUTPUT,
  WRAP,
  type Framework,
} from "@/components/quickstart/content";
import { highlight, type CodeLang } from "@/components/quickstart/highlight";
import {
  IdeaScene,
  KeyScene,
  LiveScene,
  PolicyScene,
  RegisterScene,
  RunScene,
} from "@/components/quickstart/scenes";
import { useActive } from "@/lib/active";
import { DOC_PATHS, docsUrl, mintKeyPath } from "@/lib/docs";
import { useProjects } from "@/lib/projects";
import { useUi } from "@/lib/ui";
import { useCopy } from "@/lib/use-copy";
import { cn } from "@/lib/utils";

/** A dark code block with a copy button. */
function CodeBlock({
  title,
  code,
  lang = "text",
}: {
  title: string;
  code: string;
  lang?: CodeLang;
}) {
  const [status, copyText] = useCopy();
  const tokens = useMemo(() => highlight(code, lang), [code, lang]);
  const copy = () => void copyText(code);
  const label =
    status === "copied"
      ? "Copied"
      : status === "failed"
        ? "Copy failed"
        : "Copy";
  return (
    <div className="overflow-hidden rounded-lg border border-white/10 bg-[#0a0d14] text-slate-200">
      <div className="flex items-center justify-between gap-2 px-3 pt-2 font-mono text-[11px] text-slate-500">
        <span className="truncate">{title}</span>
        <button
          type="button"
          onClick={copy}
          aria-label={`Copy ${title}`}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-slate-400 transition-colors hover:bg-white/10 hover:text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {status === "copied" ? (
            <Check className="size-3" />
          ) : (
            <Copy className="size-3" />
          )}
          {label}
        </button>
        {/* The button's aria-label hides its text, so say the result here. */}
        <span role="status" className="sr-only">
          {status === "copied"
            ? `Copied ${title}`
            : status === "failed"
              ? `Copy failed: ${title}`
              : ""}
        </span>
      </div>
      <pre className="overflow-x-auto p-3 pt-2 font-mono text-[11.5px] leading-relaxed scrollbar-thin">
        <code>{tokens}</code>
      </pre>
    </div>
  );
}

function FrameworkPicker({
  value,
  onChange,
}: {
  value: Framework;
  onChange: (f: Framework) => void;
}) {
  return (
    <ToggleGroup
      type="single"
      value={value}
      // Radix sends "" when the selected item is clicked again: keep it.
      onValueChange={(v) => v && onChange(v as Framework)}
      aria-label="Framework"
      className="grid w-full grid-cols-2 sm:grid-cols-4"
    >
      {FRAMEWORKS.map((f) => (
        <ToggleGroupItem key={f.id} value={f.id}>
          {f.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

interface Step {
  id: QuickstartStepId;
  title: string;
  body: (ctx: StepCtx) => ReactNode;
  scene: (ctx: StepCtx) => ReactNode;
  /** A page of the dashboard this step happens on. */
  action?: { label: string; to: string };
}

interface StepCtx {
  framework: Framework;
  setFramework: (f: Framework) => void;
}

// Keyed by id; QUICKSTART_STEP_IDS (content.ts) sets the order.
const STEP_DEFS: Record<QuickstartStepId, Omit<Step, "id">> = {
  idea: {
    title: "Same request, different answer",
    body: () => (
      <>
        <p>
          Your agent keeps its code. The platform keeps its <b>policy</b>: which
          role may call which tool, with which arguments. Hexgate's SDK wraps
          the agent, fetches that policy, checks every tool call before it runs,
          and records the decision.
        </p>
        <p>
          This walkthrough uses one refund tool and two roles. Support may
          refund up to $50 in USD; billing up to $500 in USD or EUR.
        </p>
        <CodeBlock title="policy.yaml" lang="yaml" code={POLICY_YAML} />
      </>
    ),
    scene: () => <IdeaScene />,
  },
  key: {
    title: "Mint an API key",
    body: () => (
      <>
        <p>
          Open <b>API keys</b> and click <b>Mint new API key</b>. Copy it
          straight away: it is shown in full only once. The key ties your agent
          to this project.
        </p>
        <CodeBlock title=".env" lang="env" code={ENV_FILE} />
      </>
    ),
    scene: () => <KeyScene />,
    action: { label: "Mint an API key", to: mintKeyPath("refund-agent-dev") },
  },
  register: {
    title: "Register and wrap your agent",
    body: ({ framework, setFramework }) => (
      <>
        <p>
          <b>Register</b> the agent so the platform knows its tools; the first
          registration creates a starter policy. Then <b>wrap</b> it with one
          adapter call. Your agent's own file doesn't change.
        </p>
        <p className="text-muted-foreground">
          The commands assume the <code>refund_agent.py</code> from chapter 1 of
          the{" "}
          <a
            href={`${docsUrl(DOC_PATHS.quickstart)}#1-the-agent`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary hover:underline"
          >
            full guide
          </a>
          ; point them at your own agent's module instead.
        </p>
        <FrameworkPicker value={framework} onChange={setFramework} />
        <CodeBlock
          title="bash"
          lang="bash"
          code={`${INSTALL}\n${REGISTER[framework]}`}
        />
        <CodeBlock
          title="guarded_agent.py"
          lang="python"
          code={WRAP[framework]}
        />
        <p className="text-muted-foreground">
          <code>HexgateContext</code> says who is asking. In your app, fill it
          from the signed-in user. Don't run it yet: the starter policy is still
          in charge.
        </p>
      </>
    ),
    scene: ({ framework }) => <RegisterScene framework={framework} />,
    action: { label: "Open Agents", to: "/agents" },
  },
  policy: {
    title: "Put your policy on the platform",
    body: () => (
      <>
        <p>
          Open <b>Policies</b>, select <code>refund_agent</code>, replace the
          starter policy with yours and save. The platform signs it and serves
          it to the agent, so the file no longer ships with your code.
        </p>
        <CodeBlock title="policy.yaml" lang="yaml" code={POLICY_YAML} />
        <p className="text-muted-foreground">
          Re-registering later updates the tool list but keeps your policy
          edits.
        </p>
      </>
    ),
    scene: () => <PolicyScene />,
    action: { label: "Open Policies", to: "/policies" },
  },
  run: {
    title: "Run it as two roles",
    body: () => (
      <>
        <p>
          Run the script. It sends the same request, "Refund $400 on order
          O-42", once as <code>support</code> and once as <code>billing</code>.
        </p>
        <CodeBlock title="bash" lang="bash" code={RUN_COMMAND} />
        <CodeBlock
          title="output · the model's wording will vary"
          code={RUN_OUTPUT}
        />
        <p>
          Under <code>support</code>, <code>refund_order</code> never runs: the
          model gets a <code>[policy_denied]</code> answer and explains it. Both
          decisions land in <b>Audit</b> with the user, role, arguments and
          reason.
        </p>
      </>
    ),
    scene: () => <RunScene />,
    action: { label: "Open Audit", to: "/audit" },
  },
  live: {
    title: "Change a rule, no redeploy",
    body: () => (
      <>
        <p>
          On <b>Policies</b>, raise the support limit to{" "}
          <code>args.amount {"<="} 500</code> and save. Run the same script
          again: the support refund now goes through. The SDK checks for a new
          signed policy at the start of each run, so nothing is redeployed.
        </p>
        <p>
          To chat with the agent in the browser and pick a role from a menu,
          start it with <code>hexgate serve</code> and open the{" "}
          <b>Playground</b>.
        </p>
      </>
    ),
    scene: () => <LiveScene />,
    action: { label: "Open Playground", to: "/playground" },
  },
};

const STEPS: Step[] = QUICKSTART_STEP_IDS.map((id) => ({
  id,
  ...STEP_DEFS[id],
}));

export function QuickstartDialog() {
  const open = useUi((s) => s.quickstartOpen);
  const close = useUi((s) => s.closeQuickstart);
  const navigate = useNavigate();
  const index = useUi((s) => Math.min(s.quickstartStep, STEPS.length - 1));
  const setIndex = useUi((s) => s.setQuickstartStep);
  // Bumped by "Replay" so the scene remounts and its animation restarts.
  const [replay, setReplay] = useState(0);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const pendingNav = useRef<string | null>(null);
  const [framework, setFramework] = useState<Framework>("langchain");

  const step = STEPS[index];
  const last = index === STEPS.length - 1;
  const ctx: StepCtx = { framework, setFramework };
  // Every step after the first happens inside a project. A new account has
  // none yet, so those steps start by offering to create one.
  const activeOrgId = useActive((s) => s.activeOrgId);
  const projectsQ = useProjects(activeOrgId);
  const needsProject = step.id !== "idea" && projectsQ.data?.length === 0;
  const [createOpen, setCreateOpen] = useState(false);

  const go = (i: number) => {
    setIndex(Math.max(0, Math.min(STEPS.length - 1, i)));
  };

  // Dismissing (Esc, overlay, ×) keeps the step for the next open; Done
  // finishes the walkthrough and starts the next one from the top.
  const onOpenChange = (next: boolean) => {
    if (!next) close();
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          // Ligatures would render <= and == as glyphs that differ from what Copy
          // puts on the clipboard.
          className="flex h-[min(680px,92vh)] max-w-5xl flex-col gap-0 overflow-hidden p-0 [font-variant-ligatures:none]"
          onCloseAutoFocus={(e) => {
            const to = pendingNav.current;
            if (!to) return;
            pendingNav.current = null;
            // Focus moves to the page we open, not back to the opener.
            e.preventDefault();
            navigate(to);
          }}
          onOpenAutoFocus={(e) => {
            // Land on the primary action rather than the first button (Replay).
            e.preventDefault();
            primaryRef.current?.focus();
          }}
          onKeyDown={(e) => {
            // Arrow keys page through the steps unless focus is in a control
            // that uses them itself.
            // Modified arrows belong to the browser (history, text navigation).
            if (e.altKey || e.metaKey || e.ctrlKey || e.shiftKey) return;
            const t = e.target as HTMLElement;
            if (t.closest("pre, input, textarea, [role='group']")) return;
            if (e.key === "ArrowRight") go(index + 1);
            if (e.key === "ArrowLeft") go(index - 1);
          }}
        >
          <header className="flex items-center gap-4 border-b border-border px-6 py-4 pr-12">
            <div className="min-w-0 flex-1">
              <DialogTitle>Quickstart</DialogTitle>
              <DialogDescription className="text-xs">
                Put an agent under a role-aware policy in six steps.
              </DialogDescription>
            </div>
            <a
              href={docsUrl(DOC_PATHS.quickstart)}
              target="_blank"
              rel="noopener noreferrer"
              className="hidden items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground sm:flex"
            >
              <BookOpen className="size-3.5" />
              Full guide
            </a>
          </header>

          <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)] md:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)] md:grid-rows-1">
            <section
              aria-label="Animation"
              className="relative order-first overflow-hidden border-b border-border bg-muted/40 p-5 md:order-last md:border-b-0 md:border-l md:p-8"
            >
              <div
                key={`${step.id}-${replay}`}
                className="mx-auto h-[220px] max-w-md md:h-full"
                aria-hidden="true"
              >
                {step.scene(ctx)}
              </div>
              <Button
                variant="ghost"
                size="sm"
                className="absolute bottom-2 right-2 h-7 gap-1.5 text-xs text-muted-foreground"
                onClick={() => setReplay((r) => r + 1)}
              >
                <RotateCcw className="size-3" />
                Replay
              </Button>
            </section>

            <section className="min-h-0 overflow-y-auto px-6 py-5 scrollbar-thin">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-primary">
                Step {index + 1} of {STEPS.length}
              </p>
              <h3 className="mb-3 mt-1 text-lg font-semibold tracking-tight">
                {step.title}
              </h3>
              <div className="flex flex-col gap-3 text-sm leading-relaxed [&_code]:font-mono [&_code]:text-[0.9em] [&_p>code]:rounded [&_p>code]:bg-muted [&_p>code]:px-1">
                {needsProject && (
                  <div className="flex flex-wrap items-center gap-3 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2.5">
                    <p className="min-w-0 flex-1">
                      <b>Create a project first.</b> Keys, agents and policies
                      live in a project, and this organization has none yet.
                    </p>
                    <Button
                      size="sm"
                      className="gap-1.5"
                      onClick={() => setCreateOpen(true)}
                    >
                      <FolderPlus className="size-3.5" />
                      Create a project
                    </Button>
                  </div>
                )}
                {step.body(ctx)}
              </div>
              {step.action ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-4 gap-1.5"
                  // Its page needs a project; the note above offers to create one.
                  disabled={needsProject}
                  onClick={() => {
                    // Keeps the step, so reopening resumes here. The page opens
                    // once the dialog has closed (onCloseAutoFocus), so a dialog
                    // that page opens (Mint API key) isn't fighting this one's
                    // focus scope on the way out.
                    pendingNav.current = step.action!.to;
                    close();
                  }}
                >
                  {step.action.label}
                  <ArrowRight className="size-3.5" />
                </Button>
              ) : null}
            </section>
          </div>

          <footer className="flex items-center justify-between gap-3 border-t border-border px-6 py-3">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => go(index - 1)}
              disabled={index === 0}
              className="gap-1.5"
            >
              <ArrowLeft className="size-3.5" />
              Back
            </Button>
            <ol className="flex items-center gap-1.5" aria-label="Steps">
              {STEPS.map((s, i) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => go(i)}
                    aria-label={`Step ${i + 1}: ${s.title}`}
                    aria-current={i === index ? "step" : undefined}
                    className={cn(
                      "block h-1.5 rounded-full transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      i === index
                        ? "w-6 bg-primary"
                        : i < index
                          ? "w-1.5 bg-primary/50"
                          : "w-1.5 bg-border",
                    )}
                  />
                </li>
              ))}
            </ol>
            {last ? (
              <Button
                ref={primaryRef}
                size="sm"
                onClick={() => close({ reset: true })}
              >
                Done
              </Button>
            ) : (
              <Button
                ref={primaryRef}
                size="sm"
                onClick={() => go(index + 1)}
                className="gap-1.5"
              >
                Next
                <ArrowRight className="size-3.5" />
              </Button>
            )}
          </footer>
        </DialogContent>
      </Dialog>
      {/* Beside the quickstart, not inside its content: keys pressed in this
        dialog would otherwise bubble to the quickstart's arrow-key paging. */}
      <CreateProjectDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}
