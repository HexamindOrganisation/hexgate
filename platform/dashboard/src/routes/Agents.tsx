import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  MATRIX_MODE_TONE,
  usePolicyFiles,
  usePolicyMatrix,
} from "@/lib/policy_files";
import { ENTRY_FILE } from "@/lib/file_tree";
import { useAgentSelection } from "@/lib/agent_param";
import {
  Ban,
  BookOpen,
  ScrollText,
  FileText,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
  api,
  type AgentManifestView,
  type GuardDefinition,
  type InputSchema,
  type PolicyMatrixCell,
  type SkillDefinition,
  type SkillResources,
  type ToolDefinition,
} from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { Badge } from "@/components/ui/badge";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DocsLink } from "@/components/DocsLink";
import { DOC_PATHS } from "@/lib/docs";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/components/ui/page-header";
import { auditUrl } from "@/lib/audit-filters";
import { useAuditSummary24h } from "@/lib/audit-summary";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * /agents — read-only manifest view.
 *
 * Renders each agent's *registered* manifest, reconstructed from the
 * JSON snapshot stored on the latest AgentVersion row in Postgres (not
 * from the legacy agent.yaml text). When an Agent row exists but no
 * version has been registered yet (e.g. YAML-seeded fixtures), the view
 * degrades to an empty-state telling the user to register first.
 *
 * Editing happens elsewhere: policy authoring lives in /policies,
 * manifest registration goes through the SDK's ``hexgate register``.
 */
export function AgentsPage() {
  const scope = useProjectScoped();
  const manifests = useQuery({
    queryKey: ["agent-manifests", scope.projectId],
    queryFn: () => api.listAgentManifests(scope.projectId as string),
    enabled: !!scope.projectId,
  });
  // ?agent= drives selection so a link from /policies (or a shared URL
  // /agents?agent=support_bot) lands on the right agent. Falls back to
  // the first agent when the URL is unset or references a stale name.
  // resetOn=projectId means the URL clears on a real project change,
  // but NOT on the initial mount — so /agents?agent=beta from an
  // incoming link isn't stomped before the user sees it.
  const { selected: selectedName, set: setSelected } = useAgentSelection(
    manifests.data,
    { resetOn: scope.projectId },
  );

  const active = manifests.data?.find((m) => m.name === selectedName);
  // 24h activity per agent; shares Home's cache entry.
  const summary = useAuditSummary24h(scope.projectId, { poll: true });
  // Roles x tools for the selected agent, as the engine tabulates them.
  // Only projects with a policy.yaml compose; others have no matrix.
  const policyFiles = usePolicyFiles(scope.projectId);
  const modular = !!policyFiles.data?.some((f) => f.name === ENTRY_FILE);
  // Manifest tools the policy doesn't list get the engine's fallback too.
  const toolNames = useMemo(
    () => active?.manifest?.tools?.map((t) => t.name) ?? [],
    [active],
  );
  const matrix = usePolicyMatrix(
    scope.projectId,
    active?.name ?? "",
    modular && !!active,
    toolNames,
  ).data;
  const roleModes = useMemo(() => {
    if (!matrix) return undefined;
    return (tool: string): RoleMode[] =>
      matrix.roles.map((role) => {
        const cell = matrix.cells[tool]?.[role];
        return {
          role,
          mode: cell?.mode,
          constraint: cell?.constraint ?? null,
          defaulted: !matrix.tools.includes(tool),
        };
      });
  }, [matrix]);
  const byAgent = useMemo(
    () => new Map(summary.data?.by_agent.map((r) => [r.key, r])),
    [summary.data],
  );
  // null until the summary lands; an agent absent from it had no calls.
  const activityOf = (name: string) =>
    summary.data
      ? (byAgent.get(name) ?? { all: 0, deny: 0, needs_approval: 0 })
      : null;
  const activeActivity = active ? activityOf(active.name) : null;
  const canManageBans = useCanManageBans();

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="agents" />;
  }

  return (
    <div className="mx-auto max-w-[1240px]">
      <PageHeader
        title="Agents"
        description={
          <>
            Manifests registered from the SDK with{" "}
            <span className="font-mono text-foreground">hexgate register</span>.
            Read-only here; policy lives in Policies.
          </>
        }
        actions={
          <DocsLink path={DOC_PATHS.registerAgent} label="Register an agent" />
        }
      />

      <div className="grid items-start gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
        <Card className="overflow-hidden p-0">
          <nav aria-label="Agents" className="flex flex-col">
            {manifests.isPending ? (
              <div className="px-4 py-5 text-meta text-muted-foreground">
                Loading agents…
              </div>
            ) : manifests.isError && !manifests.data ? (
              <div className="px-4 py-5 text-meta text-muted-foreground">
                Couldn't load agents.
              </div>
            ) : (manifests.data ?? []).length ? (
              (manifests.data ?? []).map((a) => {
                const calls = activityOf(a.name)?.all;
                const on = a.name === selectedName;
                return (
                  <button
                    key={a.name}
                    type="button"
                    aria-current={on || undefined}
                    onClick={() => setSelected(a.name)}
                    className={cn(
                      "flex flex-col gap-1 border-t border-border/60 px-4 py-3 text-left transition-colors first:border-t-0 hover:bg-accent/50",
                      on && "bg-primary/10 hover:bg-primary/10",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-2 font-mono text-[13px] font-medium">
                      <span
                        className={cn(
                          "size-1.5 shrink-0 rounded-full",
                          a.manifest ? "bg-success" : "bg-muted-foreground/40",
                        )}
                      />
                      <span className="truncate">{a.name}</span>
                    </span>
                    <span className="pl-3.5 text-meta text-muted-foreground">
                      {a.manifest
                        ? pluralise(a.manifest.tools?.length ?? 0, "tool")
                        : "not registered"}
                      {calls !== undefined &&
                        ` · ${pluralise(calls, "call")} in 24h`}
                    </span>
                  </button>
                );
              })
            ) : (
              <div className="px-4 py-5 text-meta text-muted-foreground">
                No agents yet. Register one with{" "}
                <span className="font-mono text-foreground">
                  hexgate register
                </span>
                .
              </div>
            )}
          </nav>
        </Card>

        {active ? (
          <div className="space-y-4">
            <Card className="p-5">
              <div className="flex flex-wrap items-start gap-4">
                <div className="min-w-0 flex-1 basis-[320px]">
                  <div className="flex flex-wrap items-center gap-2.5">
                    <h2 className="font-mono text-title-sm font-semibold">
                      {active.name}
                    </h2>
                    <Badge variant="outline" className="font-mono text-[11px]">
                      {active.version !== null
                        ? `v${active.version}`
                        : "not registered"}
                    </Badge>
                  </div>
                  {active.manifest?.description?.trim() && (
                    <p className="mt-1.5 max-w-[640px] text-sm text-muted-foreground">
                      {active.manifest.description.trim()}
                    </p>
                  )}
                  <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-meta text-muted-foreground">
                    <div>
                      <dt className="inline">Model </dt>
                      <dd className="inline font-mono text-foreground">
                        {active.manifest?.model?.trim() || "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="inline">Framework </dt>
                      <dd className="inline font-mono text-foreground">
                        {active.manifest?.framework?.trim() || "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="inline">Updated </dt>
                      <dd className="inline text-foreground">
                        {formatDate(active.updated_at)}
                      </dd>
                    </div>
                  </dl>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    asChild
                    variant="outline"
                    size="sm"
                    className="gap-1.5"
                  >
                    <Link
                      to={`/policies?agent=${encodeURIComponent(active.name)}`}
                    >
                      <ShieldCheck className="size-3.5" />
                      Edit policy
                    </Link>
                  </Button>
                  <Button
                    asChild
                    variant="outline"
                    size="sm"
                    className="gap-1.5"
                  >
                    <Link to={auditUrl({ agent: active.name, range: "24h" })}>
                      <ScrollText className="size-3.5" />
                      View decisions
                    </Link>
                  </Button>
                  {canManageBans && (
                    <Button
                      asChild
                      variant="outline"
                      size="sm"
                      className="gap-1.5 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    >
                      <Link
                        to={`/bans?ban_agent=${encodeURIComponent(active.name)}`}
                      >
                        <Ban className="size-3.5" />
                        Ban agent
                      </Link>
                    </Button>
                  )}
                </div>
              </div>
              {summary.isError && !summary.data ? (
                <p className="mt-4 border-t border-border/60 pt-4 text-meta text-muted-foreground">
                  Couldn't load the last 24 hours of activity.
                </p>
              ) : (
                activeActivity && (
                  <div className="mt-4 grid grid-cols-3 border-t border-border/60 pt-4 text-meta">
                    {(
                      [
                        {
                          label: "Calls · 24h",
                          value: activeActivity.all,
                          className: "",
                        },
                        {
                          label: "Denied",
                          value: activeActivity.deny,
                          className: "text-deny",
                        },
                        {
                          label: "Sent for approval",
                          value: activeActivity.needs_approval,
                          className: "text-approval",
                        },
                      ] satisfies {
                        label: string;
                        value: number;
                        className: string;
                      }[]
                    ).map((stat) => (
                      <div key={stat.label}>
                        <div className="text-muted-foreground">
                          {stat.label}
                        </div>
                        <div
                          className={cn(
                            "mt-1 text-[20px] font-semibold tabular-nums",
                            stat.className,
                          )}
                        >
                          {stat.value.toLocaleString()}
                        </div>
                      </div>
                    ))}
                  </div>
                )
              )}
            </Card>
            <ManifestView agent={active} roleModes={roleModes} />
          </div>
        ) : (
          manifests.isSuccess &&
          manifests.data.length > 0 && (
            <Card className="grid place-items-center p-12 text-sm text-muted-foreground">
              No agents to inspect.
            </Card>
          )
        )}
      </div>
    </div>
  );
}

function ManifestView({
  agent,
  roleModes,
}: {
  agent: AgentManifestView;
  roleModes?: (tool: string) => RoleMode[];
}) {
  return (
    <div className="space-y-4">
      <ToolsSection
        tools={agent.manifest?.tools ?? []}
        unregistered={agent.manifest === null}
        roleModes={roleModes}
      />
      <GuardsSection
        guards={agent.manifest?.guards ?? null}
        unregistered={agent.manifest === null}
      />
      <SkillsSection
        skills={agent.manifest?.skills ?? null}
        unregistered={agent.manifest === null}
      />
      <SystemPromptSection
        prompt={agent.manifest?.system_prompt ?? null}
        unregistered={agent.manifest === null}
      />
    </div>
  );
}

/** How each role treats one tool, from the composed policy. */
interface RoleMode {
  role: string;
  mode: PolicyMatrixCell["mode"] | undefined;
  /** The engine's condition text, when the cell is constrained. */
  constraint: string | null;
  /** Not listed for the role: its default applies. */
  defaulted: boolean;
}

function ToolsSection({
  tools,
  unregistered,
  roleModes,
}: {
  tools: ToolDefinition[];
  unregistered: boolean;
  roleModes?: (tool: string) => RoleMode[];
}) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <div className="px-5 py-4 border-b border-border flex items-center gap-2">
        <Wrench className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Tools</span>
        <Badge variant="outline" className="ml-1 font-mono text-[11px]">
          {tools.length}
        </Badge>
      </div>
      {tools.length === 0 ? (
        <p className="px-5 py-4 text-xs text-muted-foreground">
          {unregistered
            ? "Agent not registered yet — run `hexgate register` to populate."
            : "No tools declared."}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {tools.map((t) => (
            <ToolRow key={t.name} tool={t} roles={roleModes?.(t.name)} />
          ))}
        </ul>
      )}
    </section>
  );
}

function GuardsSection({
  guards,
  unregistered,
}: {
  guards: GuardDefinition[] | null;
  unregistered: boolean;
}) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <div className="px-5 py-4 border-b border-border flex items-center gap-2">
        <ShieldCheck className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Guards</span>
        {guards !== null && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {guards.length}
          </Badge>
        )}
      </div>
      {guards === null || guards.length === 0 ? (
        <p className="px-5 py-4 text-xs text-muted-foreground">
          {unregistered
            ? "Agent not registered yet — run `hexgate register` to populate."
            : "No guards declared."}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {guards.map((g) => (
            <GuardRow key={g.name} guard={g} />
          ))}
        </ul>
      )}
    </section>
  );
}

function GuardRow({ guard }: { guard: GuardDefinition }) {
  return (
    <li className="px-5 py-4 space-y-2">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-sm text-foreground">{guard.name}</span>
        <Badge variant="outline" className="font-mono text-[10px] py-0">
          {guard.position}
        </Badge>
        {guard.kind === "official" && (
          <Badge variant="outline" className="font-mono text-[10px] py-0">
            plugin
          </Badge>
        )}
        {guard.observe && (
          <Badge variant="outline" className="font-mono text-[10px] py-0">
            observe
          </Badge>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {guard.position === "before"
          ? "Runs before the tool call"
          : "Runs on the tool result"}
        {" · "}
        {guard.tool_names === null
          ? "every tool"
          : `scoped to ${guard.tool_names.join(", ")}`}
      </p>
    </li>
  );
}

function SkillsSection({
  skills,
  unregistered,
}: {
  skills: SkillDefinition[] | null;
  unregistered: boolean;
}) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <div className="px-5 py-4 border-b border-border flex items-center gap-2">
        <BookOpen className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Skills</span>
        {/* A count of 0 would assert the same thing the copy below refuses to. */}
        {skills !== null && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {skills.length}
          </Badge>
        )}
      </div>
      {skills === null || skills.length === 0 ? (
        <p className="px-5 py-4 text-xs text-muted-foreground">
          {emptySkillsCopy(skills, unregistered)}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {skills.map((s) => (
            <SkillRow key={s.name} skill={s} />
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Null is not emptiness — the same distinction SkillResourceDetails draws one
 * level down. Nothing enumerated this agent's skills (its framework has no
 * skill concept, or it was registered before the SDK discovered any), so
 * "No skills declared." would assert something the manifest never said.
 */
function emptySkillsCopy(
  skills: SkillDefinition[] | null,
  unregistered: boolean,
): string {
  if (unregistered) {
    return "Agent not registered yet — run `hexgate register` to populate.";
  }
  return skills === null
    ? "Skills not enumerated at registration."
    : "No skills declared.";
}

function SystemPromptSection({
  prompt,
  unregistered,
}: {
  prompt: string | null;
  unregistered: boolean;
}) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <div className="px-5 py-4 border-b border-border flex items-center gap-2">
        <FileText className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">System prompt</span>
        {prompt && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {prompt.length} chars
          </Badge>
        )}
      </div>
      {prompt ? (
        <details className="group" open>
          <summary className="px-5 py-2 text-[11px] text-muted-foreground cursor-pointer select-none">
            show / hide
          </summary>
          <pre className="px-5 pb-4 text-[12px] font-mono text-foreground whitespace-pre-wrap break-words">
            {prompt}
          </pre>
        </details>
      ) : (
        <p className="px-5 py-4 text-xs text-muted-foreground">
          {unregistered
            ? "Agent not registered yet — run `hexgate register` to populate."
            : "No system prompt declared."}
        </p>
      )}
    </section>
  );
}

function chipTitle(r: RoleMode): string | undefined {
  const notes = [r.defaulted && "not listed, default applies", r.constraint];
  const text = notes.filter(Boolean).join(", ");
  return text ? `${r.role}: ${text}` : undefined;
}

function ToolRow({
  tool,
  roles,
}: {
  tool: ToolDefinition;
  roles?: RoleMode[];
}) {
  return (
    <li className="px-5 py-4 space-y-2">
      <div className="flex flex-wrap items-baseline gap-3">
        <span className="font-mono text-sm text-foreground">{tool.name}</span>
        {roles && roles.length > 0 && (
          <span className="ml-auto flex flex-wrap gap-1.5">
            {roles.map((r) => (
              <span
                key={r.role}
                title={chipTitle(r)}
                className={cn(
                  "rounded-md px-2 py-0.5 text-[11px] font-medium",
                  r.mode ? MATRIX_MODE_TONE[r.mode] : "text-muted-foreground",
                  r.defaulted && "opacity-50",
                )}
              >
                <span className="font-mono">{r.role}</span> {r.mode ?? "—"}
                {r.constraint && "*"}
              </span>
            ))}
          </span>
        )}
      </div>
      {tool.description?.trim() ? (
        <p className="text-xs text-muted-foreground">{tool.description}</p>
      ) : null}
      <InputSchemaDetails schema={tool.input_schema} />
    </li>
  );
}

function InputSchemaDetails({ schema }: { schema: InputSchema }) {
  const entries = Object.entries(schema.properties);
  const required = new Set(schema.required);
  return (
    <details className="rounded-md border border-border bg-background/40 group">
      <summary className="px-3 py-2 cursor-pointer flex items-center gap-2 text-[11px] text-muted-foreground select-none">
        <span>inputs</span>
        <Badge variant="outline" className="font-mono text-[10px] py-0">
          {entries.length}
        </Badge>
      </summary>
      {entries.length === 0 ? (
        <p className="px-3 pb-2 text-[11px] text-muted-foreground">
          No inputs.
        </p>
      ) : (
        <ul className="px-3 pb-2 space-y-1 text-[11px] font-mono">
          {entries.map(([key, prop]) => (
            <li key={key} className="flex items-baseline gap-2">
              <span className="text-foreground">{key}</span>
              <span className="text-muted-foreground">: {prop.type}</span>
              {required.has(key) && (
                <Badge variant="outline" className="text-[9px] py-0">
                  required
                </Badge>
              )}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

/** Executable code and the tools a skill adds — the two risk-carrying counts. */
function SkillRow({ skill }: { skill: SkillDefinition }) {
  const scripts = skill.resources?.scripts.length ?? 0;
  const addedTools = skill.additional_tools.length;
  return (
    <li className="px-5 py-4 space-y-2">
      <div className="flex items-baseline gap-3">
        <span className="font-mono text-sm text-foreground">{skill.name}</span>
        {scripts > 0 && (
          <Badge variant="outline" className="font-mono text-[10px] py-0">
            {pluralise(scripts, "script")}
          </Badge>
        )}
        {addedTools > 0 && (
          <Badge variant="outline" className="font-mono text-[10px] py-0">
            +{pluralise(addedTools, "tool")}
          </Badge>
        )}
      </div>
      {skill.description.trim() ? (
        <p className="text-xs text-muted-foreground">{skill.description}</p>
      ) : null}
      <SkillResourceDetails resources={skill.resources} />
    </li>
  );
}

const RESOURCE_KINDS = ["references", "assets", "scripts"] as const;

function SkillResourceDetails({
  resources,
}: {
  resources: SkillResources | null;
}) {
  // Null is not emptiness: the framework never enumerated resources, so
  // claiming the skill ships none would be a fabrication.
  if (resources === null) {
    return (
      <p className="text-[11px] text-muted-foreground">
        Resources not enumerated by this framework.
      </p>
    );
  }
  const populated = RESOURCE_KINDS.filter((kind) => resources[kind].length > 0);
  const total = populated.reduce(
    (sum, kind) => sum + resources[kind].length,
    0,
  );
  return (
    <details className="rounded-md border border-border bg-background/40 group">
      <summary className="px-3 py-2 cursor-pointer flex items-center gap-2 text-[11px] text-muted-foreground select-none">
        <span>resources</span>
        <Badge variant="outline" className="font-mono text-[10px] py-0">
          {total}
        </Badge>
      </summary>
      {populated.length === 0 ? (
        <p className="px-3 pb-2 text-[11px] text-muted-foreground">
          No resources.
        </p>
      ) : (
        <ul className="px-3 pb-2 space-y-1 text-[11px] font-mono">
          {populated.map((kind) => (
            <li key={kind} className="flex items-baseline gap-2">
              <span className="text-foreground">{kind}</span>
              <span className="text-muted-foreground">
                : {resources[kind].length}
              </span>
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}
