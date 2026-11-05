import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";
import { useSession } from "./session";
import type { DashboardData } from "./types";

const DashboardContext = createContext<{
  data: DashboardData | null;
  loading: boolean;
  error: string;
  refresh: () => void;
} | null>(null);
export function DashboardProvider({ children }: { children: ReactNode }) {
  const { session } = useSession();
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  const refresh = useCallback(() => {
    if (!session.authenticated || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    api<DashboardData>("dashboard/", { signal: controller.signal })
      .then((value) => {
        setData(value);
        setError("");
      })
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      })
      .finally(() => {
        if (pending.current === controller) pending.current = null;
        setLoading(false);
      });
  }, [session.authenticated]);
  useEffect(() => {
    if (!session.authenticated) {
      setData(null);
      return;
    }
    setLoading(true);
    refresh();
    const timer = window.setInterval(() => {
      if (!document.hidden) refresh();
    }, 30000);
    return () => {
      clearInterval(timer);
      pending.current?.abort();
      pending.current = null;
    };
  }, [refresh, session.authenticated]);
  return (
    <DashboardContext.Provider value={{ data, loading, error, refresh }}>
      {children}
    </DashboardContext.Provider>
  );
}
export function useDashboard() {
  const value = useContext(DashboardContext);
  if (!value) throw new Error("DashboardProvider is required");
  return value;
}
