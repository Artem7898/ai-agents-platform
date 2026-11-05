import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import App, { AppErrorBoundary } from "./App";
import { SessionProvider } from "./lib/session";
import "@fontsource-variable/inter";
import "./styles.css";

const router = createBrowserRouter(
  [
    {
      path: "*",
      element: (
        <SessionProvider>
          <App />
        </SessionProvider>
      ),
    },
  ],
  { basename: "/console" },
);
createRoot(document.getElementById("root")!).render(
  <AppErrorBoundary>
    <RouterProvider router={router} />
  </AppErrorBoundary>,
);
