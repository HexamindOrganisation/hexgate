import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import "@xyflow/react/dist/style.css";
import "./index.css";
import App from "./App.tsx";
import { applyTheme, useApplyTheme, useIsDark, useTheme } from "./lib/theme";

// Apply the persisted theme before the first paint so there's no flash of the
// wrong palette (zustand's persist middleware rehydrates synchronously).
applyTheme(useTheme.getState().mode, useTheme.getState().scheme);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      refetchOnWindowFocus: false,
    },
  },
});

function Root() {
  useApplyTheme();
  const dark = useIsDark();
  return (
    <QueryClientProvider client={queryClient}>
      <App />
      {/* Mounted at root so any component (even inside a modal portal) can
       * toast; theme follows the app mode. */}
      <Toaster
        theme={dark ? "dark" : "light"}
        richColors
        position="bottom-right"
      />
    </QueryClientProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
