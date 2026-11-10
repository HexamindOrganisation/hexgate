import { BookOpen, FileText, ShieldCheck, Wrench } from "lucide-react";

import { MATRIX_MODE_TONE } from "@/lib/policy_files";
import type { RoleMode } from "@/lib/role-modes";
import type {
  AgentManifestView,
  GuardDefinition,
  InputSchema,
  SkillDefinition,
  SkillResources,
  ToolDefinition,
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

/** An agent's manifest as the hub's Resources tab shows it: tools (with how
 * each role treats them), guards, skills and the system prompt. */
export function ManifestView({
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
