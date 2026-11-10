import type { PolicyMatrix, PolicyMatrixCell } from "@/lib/api";

/** One role's standing for a tool, as an agent page's chips show it. */
export interface RoleMode {
  role: string;
  mode: PolicyMatrixCell["mode"] | undefined;
  /** The engine's condition text, when the cell is constrained. */
  constraint: string | null;
  /** The policy lists the tool for no role: every chip is a fallback. */
  defaulted: boolean;
}

/** Each role's standing for a tool, from the engine's matrix. One rule for
 * every page that shows an agent's tools by role. */
export function roleModesOf(matrix: PolicyMatrix) {
  const listed = new Set(matrix.tools);
  return (tool: string): RoleMode[] => {
    const defaulted = !listed.has(tool);
    return matrix.roles.map((role) => {
      const cell = matrix.cells[tool]?.[role];
      return {
        role,
        mode: cell?.mode,
        constraint: cell?.constraint ?? null,
        defaulted,
      };
    });
  };
}
