import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** The Playground's top-bar text button: a ghost Button, focus ring included. */
export const BAR_BTN = cn(
  buttonVariants({ variant: "ghost", size: "sm" }),
  "gap-1.5 px-2 text-sm font-normal text-muted-foreground hover:text-foreground disabled:opacity-40",
);
