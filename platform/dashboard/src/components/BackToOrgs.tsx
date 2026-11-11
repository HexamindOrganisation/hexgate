import { ArrowLeft } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";

/** "Back to organizations", the same on every org page. */
export function BackToOrgs() {
  const navigate = useNavigate();
  return (
    <Button
      variant="ghost"
      size="sm"
      onClick={() => navigate("/orgs")}
      className="-ml-2"
    >
      <ArrowLeft className="size-3.5" />
      Back to organizations
    </Button>
  );
}
