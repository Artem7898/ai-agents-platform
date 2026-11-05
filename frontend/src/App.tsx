import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Link,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  Activity,
  ArrowRight,
  Bell,
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  Command,
  CreditCard,
  ExternalLink,
  LayoutDashboard,
  ListTree,
  LogOut,
  Menu,
  Plus,
  Search,
  Settings as SettingsIcon,
  ShieldCheck,
  Workflow,
  X,
} from "lucide-react";
import {
  Button,
  Card,
  ErrorNotice,
  Field,
  Modal,
  Spinner,
} from "./components/ui";
import { DashboardProvider, useDashboard } from "./lib/dashboard";
import { NewRunProvider, useNewRun } from "./lib/run-dialog";
import { useSession } from "./lib/session";
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Agents = lazy(() => import("./pages/Agents"));
const Workflows = lazy(() => import("./pages/Workflows"));
const Runs = lazy(() => import("./pages/Runs"));
const Knowledge = lazy(() => import("./pages/Knowledge"));
const Observability = lazy(() => import("./pages/Observability"));
const Billing = lazy(() => import("./pages/Billing"));
const Settings = lazy(() => import("./pages/Settings"));

const navigation = [
  {
    to: "/",
    label: "Dashboard",
    icon: LayoutDashboard,
    title: "Dashboard Overview",
    description: "Monitor agents, workflows and infrastructure",
  },
  {
    to: "/agents",
    label: "Agents",
    icon: Bot,
    title: "Your agents",
    description: "Configure models, instructions and allowed tools",
  },
  {
    to: "/workflows",
    label: "Workflow Builder",
    icon: Workflow,
    title: "Workflow Builder",
    description: "Design and manage durable agent workflows",
  },
  {
    to: "/runs",
    label: "Runs / Traces",
    icon: ListTree,
    title: "Runs & traces",
    description: "Follow execution, inspect events and resolve approvals",
  },
  {
    to: "/knowledge",
    label: "Knowledge & Memory",
    icon: BookOpen,
    title: "Knowledge & memory",
    description: "Understand your connected knowledge capabilities",
  },
  {
    to: "/observability",
    label: "Observability",
    icon: Activity,
    title: "Observability",
    description: "Inspect queue health, usage and execution signals",
  },
  {
    to: "/billing",
    label: "Billing & Usage",
    icon: CreditCard,
    title: "Billing & usage",
    description: "Manage subscriptions, consent limits and invoices",
  },
  {
    to: "/settings",
    label: "Settings",
    icon: SettingsIcon,
    title: "Workspace settings",
    description: "Manage your profile, security and runtime configuration",
  },
];

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true">
          <path
            d="M7 25L15.5 6H20L25 25H20.8L19.6 20H12.6L10.4 25Z"
            fill="white"
          />
          <path d="M14.2 16.5H18.9L17.3 10.5Z" fill="#8965d4" />
          <path
            d="M22 7L25 10M9 11L12 8"
            stroke="white"
            strokeWidth="2.3"
            strokeLinecap="round"
          />
        </svg>
      </span>
      <span>
        <strong>AgentOps</strong>
        <small>AI AGENTS PLATFORM</small>
      </span>
    </div>
  );
}

function Login() {
  const { login, error: connectionError, refresh } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(username, password);
      setPassword("");
    } catch (error) {
      setError(error instanceof Error ? error.message : "Unable to sign in.");
      setPassword("");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login-screen">
      <div className="login-brand-panel">
        <Brand />
        <div className="login-illustration">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="orbit orbit-three" />
          <div className="login-node login-node-one">
            <Bot size={25} />
          </div>
          <div className="login-node login-node-two">
            <Workflow size={27} />
          </div>
          <div className="login-node login-node-three">
            <ShieldCheck size={25} />
          </div>
          <div className="login-orb">
            <Activity size={46} strokeWidth={1.4} />
          </div>
        </div>
        <h1>
          Your agents.
          <br />
          One clear view.
        </h1>
        <p>
          Build reliable workflows. Follow every run.
          <br />
          Keep usage and decisions in sight.
        </p>
        <span className="login-panel-footer">
          A workspace for thoughtful automation.
        </span>
      </div>
      <div className="login-form-panel">
        <div className="login-mobile-brand">
          <Brand />
        </div>
        <form className="login-form" onSubmit={submit}>
          <span className="eyebrow">WELCOME TO AGENTOPS</span>
          <h2>Good to see you again.</h2>
          <p>Sign in to your workspace to continue.</p>
          <ErrorNotice message={error || connectionError} />
          {connectionError && (
            <Button
              onClick={() => {
                refresh().catch((error) => setError(error.message));
              }}
            >
              Reconnect
            </Button>
          )}
          <Field label="Username">
            <input
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
              maxLength={150}
              autoFocus
            />
          </Field>
          <Field label="Password">
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              maxLength={1024}
            />
          </Field>
          <Button
            type="submit"
            variant="primary"
            disabled={busy || Boolean(connectionError)}
            className="sign-in-button"
          >
            {busy ? <Spinner /> : "Sign in"}
            {!busy && <ArrowRight size={18} />}
          </Button>
          <p className="login-help">
            Need an account or help signing in?
            <br />
            Contact your workspace administrator.
          </p>
          <div className="login-security">
            <ShieldCheck size={15} />
            Secure session · Credentials stay in your workspace
          </div>
        </form>
      </div>
    </div>
  );
}

function SearchDialog({ close }: { close: () => void }) {
  const [query, setQuery] = useState("");
  const { data } = useDashboard();
  const navigate = useNavigate();
  const pages = navigation.filter((item) =>
    item.label.toLowerCase().includes(query.toLowerCase()),
  );
  const runs =
    data?.recent_runs.filter((run) =>
      `${run.workflow_name} ${run.id}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    ) ?? [];
  const go = (path: string) => {
    navigate(path);
    close();
  };
  return (
    <Modal title="Find your way" onClose={close}>
      <label className="search-dialog-input">
        <Search size={19} />
        <input
          autoFocus
          aria-label="Search pages or recent runs"
          placeholder="Search pages or recent runs…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <div className="search-results">
        <span className="eyebrow">PAGES</span>
        {pages.map((item) => (
          <button key={item.to} onClick={() => go(item.to)}>
            <item.icon size={18} />
            <span>{item.label}</span>
            <ArrowRight size={14} />
          </button>
        ))}
        {runs.length > 0 && <span className="eyebrow">RECENT RUNS</span>}
        {runs.map((run) => (
          <button key={run.id} onClick={() => go(`/runs/${run.id}`)}>
            <ListTree size={18} />
            <span>
              {run.workflow_name}
              <small>#{run.id.slice(0, 8)}</small>
            </span>
            <ArrowRight size={14} />
          </button>
        ))}
        {!pages.length && !runs.length && (
          <p className="muted">No matching page or recent run.</p>
        )}
      </div>
    </Modal>
  );
}

function Layout() {
  const { session, logout } = useSession();
  const { data, error: dashboardError } = useDashboard();
  const { open } = useNewRun();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [search, setSearch] = useState(false);
  const [notifications, setNotifications] = useState(false);
  const [account, setAccount] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const alertRef = useRef<HTMLDivElement>(null);
  const accountRef = useRef<HTMLDivElement>(null);
  const mobileClose = useRef<HTMLButtonElement>(null);
  const current =
    navigation.find(
      (item) => item.to !== "/" && location.pathname.startsWith(item.to),
    ) ?? navigation[0];
  const user = session.user!;
  const name = `${user.first_name} ${user.last_name}`.trim() || user.username;
  const initials = name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();
  useEffect(() => {
    setMobileOpen(false);
    setAccount(false);
    setNotifications(false);
    document.title = `${current.title} · AgentOps`;
  }, [location.pathname, current.title]);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearch(true);
      }
      if (event.key === "Escape") {
        setMobileOpen(false);
        setAccount(false);
        setNotifications(false);
      }
    };
    const click = (event: MouseEvent) => {
      if (!alertRef.current?.contains(event.target as Node))
        setNotifications(false);
      if (!accountRef.current?.contains(event.target as Node))
        setAccount(false);
    };
    document.addEventListener("keydown", keydown);
    document.addEventListener("click", click);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("click", click);
    };
  }, []);
  useEffect(() => {
    if (mobileOpen) mobileClose.current?.focus();
  }, [mobileOpen]);
  const ready =
    data?.metrics.system_health.status === "ready" && !dashboardError;
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      {mobileOpen && (
        <div
          className="navigation-backdrop"
          onClick={() => setMobileOpen(false)}
        />
      )}
      <aside
        className={`sidebar ${mobileOpen ? "sidebar-open" : ""}`}
        role={mobileOpen ? "dialog" : undefined}
        aria-modal={mobileOpen || undefined}
        aria-label="Main navigation"
      >
        <div className="sidebar-brand">
          <Brand />
          <button
            ref={mobileClose}
            className="icon-button close-navigation"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          >
            <X size={20} />
          </button>
        </div>
        <nav className="main-navigation">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `nav-item ${isActive ? "nav-active" : ""}`
              }
            >
              <item.icon size={21} strokeWidth={1.6} />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <Link className="sidebar-health" to="/observability">
            <span className={`health-dot ${ready ? "" : "health-pending"}`} />
            {ready
              ? "API & database ready"
              : data
                ? "Check workspace status"
                : "Connecting to workspace"}
          </Link>
          <Link className="sidebar-profile" to="/settings">
            <span className="avatar">{initials}</span>
            <span>
              <strong>{name}</strong>
              <small>
                {user.is_staff ? "Workspace admin" : "Workspace member"}
              </small>
            </span>
            <ChevronDown size={15} />
          </Link>
        </div>
      </aside>
      <div className="main-shell" inert={mobileOpen}>
        <header className="topbar">
          <button
            className="icon-button open-navigation"
            aria-label="Open navigation"
            onClick={() => setMobileOpen(true)}
          >
            <Menu size={22} />
          </button>
          <div className="topbar-title">
            <h1>{current.title}</h1>
            <p>{current.description}</p>
          </div>
          <div className="topbar-actions">
            <span
              className="environment-pill"
              title="This server's environment"
            >
              {session.environment === "production"
                ? "Production"
                : "Development"}
              <span className="environment-dot" />
            </span>
            <button
              className="icon-button"
              aria-label="Search workspace"
              onClick={() => setSearch(true)}
              title="Search · Ctrl/⌘ K"
            >
              <Search size={21} />
            </button>
            <div className="popover-anchor" ref={alertRef}>
              <button
                className="icon-button notification-button"
                aria-label="Workspace alerts"
                aria-expanded={notifications}
                onClick={() => {
                  setNotifications(!notifications);
                  setAccount(false);
                }}
              >
                <Bell size={20} />
                {Boolean(data?.alerts.length) && (
                  <span className="notification-count">
                    {data!.alerts.length > 9 ? "9+" : data!.alerts.length}
                  </span>
                )}
              </button>
              {notifications && (
                <div className="popover alerts-popover">
                  <div className="popover-heading">
                    <strong>Workspace alerts</strong>
                    <span>{data?.alerts.length ?? 0}</span>
                  </div>
                  {data?.alerts.length ? (
                    data.alerts.map((alert) => (
                      <Link to={alert.href} key={alert.id}>
                        <span
                          className={`alert-indicator alert-${alert.kind}`}
                        />
                        <div>
                          <strong>{alert.title}</strong>
                          <small>{alert.description}</small>
                        </div>
                        <ExternalLink size={13} />
                      </Link>
                    ))
                  ) : (
                    <div className="popover-empty">
                      <Check size={20} />
                      No workspace alerts right now.
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="popover-anchor account-anchor" ref={accountRef}>
              <button
                className="avatar avatar-button"
                aria-label="Account menu"
                aria-expanded={account}
                onClick={() => {
                  setAccount(!account);
                  setNotifications(false);
                }}
              >
                {initials}
              </button>
              {account && (
                <div className="popover account-popover">
                  <strong>{name}</strong>
                  <small>{user.username}</small>
                  <button
                    onClick={() => {
                      navigate("/settings");
                      setAccount(false);
                    }}
                  >
                    <SettingsIcon size={16} />
                    Account settings
                  </button>
                  <button
                    onClick={() => {
                      logout().catch((error) => setLogoutError(error.message));
                    }}
                  >
                    <LogOut size={16} />
                    Sign out
                  </button>
                </div>
              )}
            </div>
            <Button
              variant="primary"
              onClick={() => open()}
              className="new-run-button"
            >
              <Plus size={19} />
              <span>New run</span>
            </Button>
          </div>
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>
          <ErrorNotice message={logoutError} />
          <Suspense
            fallback={
              <div className="dashboard-loading">
                <Spinner />
                <span>Opening page…</span>
              </div>
            }
          >
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/agents" element={<Agents />} />
              <Route path="/workflows" element={<Workflows />} />
              <Route path="/runs" element={<Runs />} />
              <Route path="/runs/:id" element={<Runs />} />
              <Route path="/knowledge" element={<Knowledge />} />
              <Route path="/observability" element={<Observability />} />
              <Route path="/billing" element={<Billing />} />
              <Route path="/settings" element={<Settings />} />
              <Route
                path="*"
                element={
                  <Card>
                    <div className="empty-state">
                      <Command size={30} />
                      <h2>This page does not exist</h2>
                      <p>Choose a section from the navigation to continue.</p>
                      <Link className="button button-primary" to="/">
                        Back to dashboard
                      </Link>
                    </div>
                  </Card>
                }
              />
            </Routes>
          </Suspense>
        </main>
      </div>
      {search && <SearchDialog close={() => setSearch(false)} />}
    </div>
  );
}

export class AppErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="app-failure">
        <Card>
          <h1>We could not display this page.</h1>
          <p>Your saved workspace data is still on the server.</p>
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload workspace
          </Button>
        </Card>
      </div>
    ) : (
      this.props.children
    );
  }
}

export default function App() {
  const { session, loading } = useSession();
  if (loading)
    return (
      <div className="boot-screen">
        <Brand />
        <Spinner />
        <span>Opening your workspace…</span>
      </div>
    );
  if (!session.authenticated || !session.user) return <Login />;
  return (
    <DashboardProvider>
      <NewRunProvider>
        <Layout />
      </NewRunProvider>
    </DashboardProvider>
  );
}
