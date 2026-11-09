import { ArrowRight, Bot, Files, ScrollText } from "lucide-react";
import { Link } from "react-router-dom";

import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { useProjectScoped } from "@/lib/active";
import { PageHeader } from "@/components/ui/page-header";

// Starting points until the full overview (needs attention, today, live
// decisions) lands. Audit leads: it is where the day's work is.
const SHORTCUTS = [
  {
    to: "/audit",
    icon: ScrollText,
    title: "Audit",
    body: "Every policy decision, who asked and why it was allowed or denied.",
  },
  {
    to: "/policies",
    icon: Files,
    title: "Policies",
    body: "Edit what each role may do with each tool.",
  },
  {
    to: "/agents",
    icon: Bot,
    title: "Agents",
    body: "The agents registered to this project and their tools.",
  },
];

export function HomePage() {
  const scope = useProjectScoped();

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="project overview" />;
  }

  return (
    <div className="mx-auto max-w-[1240px]">
      <PageHeader
        title="Home"
        description={
          scope.projectName && (
            <>
              Overview of{" "}
              <span className="font-mono text-foreground">
                {scope.projectName}
              </span>
            </>
          )
        }
      />
      <div className="grid gap-4 md:grid-cols-3">
        {SHORTCUTS.map(({ to, icon: Icon, title, body }) => (
          <Link
            key={to}
            to={to}
            className="group flex flex-col gap-3 rounded-xl border border-border bg-card p-5 transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="grid size-9 place-items-center rounded-lg bg-muted text-foreground">
              <Icon className="size-4" />
            </span>
            <span>
              <span className="flex items-center gap-1.5 text-body font-semibold">
                {title}
                <ArrowRight className="size-3.5 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
              </span>
              <span className="mt-1 block text-meta text-muted-foreground">
                {body}
              </span>
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
