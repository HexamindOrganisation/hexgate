const KNOWN = ["advisor", "junior_handler", "senior_handler"];

export function RolePill({ role }: { role: string }) {
  const cls = KNOWN.includes(role) ? `role-${role}` : "role-other";
  return <span className={`pill ${cls}`}>{role.replace("_", " ")}</span>;
}
