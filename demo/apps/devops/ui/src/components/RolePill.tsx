const KNOWN = ["viewer", "engineer", "release"];

export function RolePill({ role }: { role: string }) {
  const cls = KNOWN.includes(role) ? `role-${role}` : "role-other";
  return <span className={`pill ${cls}`}>{role}</span>;
}
