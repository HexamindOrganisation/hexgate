import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import "@xyflow/react/dist/style.css";
import "./index.css";
import App from "./App.tsx";

document.documentElement.classList.add("dark");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      {/* Bottom-right by default; theme="dark" matches the rest of the
       * dashboard. Mounted at root so any component (even inside a
       * modal portal) can toast.success / toast.error. */}
      <Toaster theme="dark" richColors position="bottom-right" />
    </QueryClientProvider>
  </StrictMode>,
);
