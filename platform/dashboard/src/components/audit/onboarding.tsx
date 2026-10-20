import { ArrowRight, KeyRound, MessageSquareCode } from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DOC_PATHS, docsUrl } from "@/lib/docs";

// The real commands from the README quickstart; the key is a placeholder.
const COMMANDS = ["pip install hexgate", "export HEXGATE_API_KEY=<your key>"];

/** Shown instead of empty charts when the range holds no decisions and no
 * filter is set: the next steps to get the first decision recorded. */
export function AuditOnboarding({ onWiden }: { onWiden?: () => void }) {
  return (
    <Card className="grid overflow-hidden md:grid-cols-2">
      <div className="flex flex-col gap-5 p-7">
        <div>
          <h2 className="text-body font-semibold">
            No decisions in this range
          </h2>
          <p className="mt-1 text-meta text-muted-foreground">
            Every tool call your agents make lands here, with who asked, which
            role decided and why. If nothing is connected yet:
          </p>
        </div>
        <ol className="flex flex-col gap-4">
          <li className="flex gap-3">
            <span className="grid size-6 shrink-0 place-items-center rounded-full border border-border text-xs font-semibold">
              1
            </span>
            <div className="flex-1">
              <div className="text-sm font-semibold">Create an API key</div>
              <p className="mt-0.5 text-meta text-muted-foreground">
                It scopes the SDK to this project.
              </p>
              <Button asChild size="sm" className="mt-2.5 gap-1.5">
                <Link to="/tokens">
                  <KeyRound className="size-3.5" />
                  API keys
                </Link>
              </Button>
            </div>
          </li>
          <li className="flex gap-3">
            <span className="grid size-6 shrink-0 place-items-center rounded-full border border-border text-xs font-semibold">
              2
            </span>
            <div className="flex-1">
              <div className="text-sm font-semibold">
                Run one gated tool call
              </div>
              <p className="mt-0.5 text-meta text-muted-foreground">
                From your agent with the key set, or from the Playground.
              </p>
              <div className="mt-2.5 flex flex-wrap gap-2">
                <Button asChild size="sm" variant="outline" className="gap-1.5">
                  <Link to="/playground">
                    <MessageSquareCode className="size-3.5" />
                    Open Playground
                  </Link>
                </Button>
                <Button asChild size="sm" variant="ghost">
                  <a
                    href={docsUrl(DOC_PATHS.tokens)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Read the docs
                  </a>
                </Button>
              </div>
            </div>
          </li>
        </ol>
        {onWiden && (
          <button
            type="button"
            onClick={onWiden}
            className="flex w-fit items-center gap-1.5 text-meta font-medium text-primary hover:underline"
          >
            Look at the last 90 days instead
            <ArrowRight className="size-3.5" />
          </button>
        )}
      </div>
      <div className="flex flex-col gap-3 bg-[#0E1320] p-7 font-mono text-[13px] leading-7 text-[#D6DCE8]">
        <span className="text-xs text-[#8792A8]">terminal</span>
        <pre className="m-0 whitespace-pre-wrap">
          {COMMANDS.map((c) => (
            <div key={c}>
              <span className="select-none text-[#6E7A92]">$ </span>
              {c}
            </div>
          ))}
        </pre>
      </div>
    </Card>
  );
}
