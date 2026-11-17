/**
 * Home's "Get started" checklist: the quickstart's platform steps, ticked off
 * from the project's real state (a key minted, an agent registered, a decision
 * recorded). Each step links to where it happens — the key step straight into
 * the "Mint API key" window — or opens the quickstart dialog at that step.
 *
 * Renders nothing until every signal is known, and nothing once they're all
 * done, so a set-up project never sees it flash by.
 */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, Rocket } from "lucide-react";

import { QUICKSTART_STEP } from "@/components/quickstart/content";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { mintKeyPath } from "@/lib/docs";
import { useUi } from "@/lib/ui";
import { cn } from "@/lib/utils";

export interface SetupState {
  /** `undefined` while unknown (loading or failed). */
  hasKey: boolean | undefined;
  hasAgent: boolean | undefined;
  hasDecision: boolean | undefined;
}

interface Item {
  key: string;
  title: string;
  sub: ReactNode;
  /** `null` for a step the dashboard can't check (the policy edit). */
  done: boolean | null;
  cta: ReactNode;
}

const ctaClass =
  "inline-flex items-center gap-1 text-meta font-medium text-primary hover:underline";

export function GetStarted({ hasKey, hasAgent, hasDecision }: SetupState) {
  const openQuickstart = useUi((s) => s.openQuickstart);
  if (
    hasKey === undefined ||
    hasAgent === undefined ||
    hasDecision === undefined
  )
    return null;
  if (hasKey && hasAgent && hasDecision) return null;

  const showMe = (step: number) => (
    <button
      type="button"
      className={ctaClass}
      onClick={() => openQuickstart(step)}
    >
      Show me
      <ArrowRight className="size-3.5" />
    </button>
  );

  const items: Item[] = [
    {
      key: "key",
      title: "Mint an API key",
      sub: "It ties your agent to this project.",
      done: hasKey,
      cta: (
        <Link to={mintKeyPath()} className={ctaClass}>
          {hasKey ? "Mint another" : "Mint a key"}
          <ArrowRight className="size-3.5" />
        </Link>
      ),
    },
    {
      key: "register",
      title: "Register and wrap your agent",
      sub: (
        <>
          <span className="font-mono">hexgate register</span>, then one adapter
          call.
        </>
      ),
      done: hasAgent,
      cta: showMe(QUICKSTART_STEP.register),
    },
    {
      key: "policy",
      title: "Put your policy on the platform",
      sub: "Replace the starter policy and save.",
      done: null,
      cta: (
        <Link to="/policies" className={ctaClass}>
          Edit policies
          <ArrowRight className="size-3.5" />
        </Link>
      ),
    },
    {
      key: "decision",
      title: "Run it and see a decision",
      sub: "Every allow and deny lands in Audit.",
      done: hasDecision,
      cta: showMe(QUICKSTART_STEP.run),
    },
  ];
  const checkable = items.filter((i) => i.done !== null);
  const doneCount = checkable.filter((i) => i.done).length;

  return (
    <Card className="overflow-hidden p-0" aria-labelledby="get-started-title">
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-3.5">
        <h2 id="get-started-title" className="text-body font-semibold">
          Get started{" "}
          <span className="font-medium text-muted-foreground">
            · {doneCount} of {checkable.length} done
          </span>
        </h2>
        <Button
          size="sm"
          className="ml-auto gap-1.5"
          onClick={() => openQuickstart(QUICKSTART_STEP.idea)}
        >
          <Rocket className="size-3.5" />
          Watch the quickstart
        </Button>
      </div>
      <ol className="grid sm:grid-cols-2 lg:grid-cols-4">
        {items.map((item, i) => (
          <li
            key={item.key}
            className="flex gap-3 border-t border-border/60 px-5 py-4 first:border-t-0 sm:[&:nth-child(2)]:border-t-0 lg:border-l lg:border-t-0 lg:first:border-l-0"
          >
            <span
              className={cn(
                "grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold",
                item.done
                  ? "bg-success/15 text-success"
                  : "bg-primary/10 text-primary",
              )}
              aria-label={
                item.done === null ? undefined : item.done ? "Done" : "To do"
              }
            >
              {item.done ? <Check className="size-3.5" /> : i + 1}
            </span>
            <div className="flex min-w-0 flex-col gap-1">
              <span
                className={cn(
                  "text-sm font-semibold",
                  item.done && "text-muted-foreground line-through",
                )}
              >
                {item.title}
              </span>
              <span className="text-meta text-muted-foreground">
                {item.sub}
              </span>
              <span className="mt-0.5">{item.cta}</span>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  );
}
