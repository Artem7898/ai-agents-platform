import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, setCsrfToken } from "./api";
import type { Session } from "./types";

const anonymous: Session = {
  authenticated: false,
  csrf_token: "",
  user: null,
  environment: "development",
  timezone: "UTC",
  billing: { enabled: false, mode: "test" },
  capabilities: { knowledge_search_configured: false },
};
const SessionContext = createContext<{
  session: Session;
  loading: boolean;
  error: string;
  refresh: () => Promise<Session>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
} | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session>(anonymous);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    const next = await api<Session>("session/");
    setCsrfToken(next.csrf_token);
    setSession(next);
    setError("");
    return next;
  }, []);
  useEffect(() => {
    refresh()
      .catch((error) => setError(error.message))
      .finally(() => setLoading(false));
    const expired = () => {
      setSession(anonymous);
      refresh().catch(() => {});
    };
    window.addEventListener("console:session-expired", expired);
    return () => window.removeEventListener("console:session-expired", expired);
  }, [refresh]);
  const login = async (username: string, password: string) => {
    await api("login/", { method: "POST", body: { username, password } });
    await refresh();
  };
  const logout = async () => {
    await api("logout/", { method: "POST", body: {} });
    await refresh();
  };
  return (
    <SessionContext.Provider
      value={{ session, loading, error, refresh, login, logout }}
    >
      {children}
    </SessionContext.Provider>
  );
}

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("SessionProvider is required");
  return value;
}
