const KNOWN = ["trainee", "teller", "branch_manager"];

export function RolePill({ role }: { role: string }) {
  const cls = KNOWN.includes(role) ? `role-${role}` : "role-other";
  return <span className={`pill ${cls}`}>{role.replace("_", " ")}</span>;
}
