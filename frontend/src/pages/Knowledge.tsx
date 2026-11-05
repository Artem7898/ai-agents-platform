import {
  BookOpen,
  Database,
  FileSearch,
  HardDrive,
  Info,
  LockKeyhole,
  Network,
  ShieldCheck,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useSession } from "../lib/session";
import { Badge, Card, PageHeader } from "../components/ui";
import "./agents-settings.css";

export default function Knowledge() {
  const { session } = useSession();
  const configured = Boolean(session.capabilities.knowledge_search_configured);
  return (
    <div className="as-page">
      <PageHeader
        title="Knowledge & memory"
        description="Understand the knowledge services your agents can actually access."
      />
      <div className="as-info-banner">
        <Info size={20} />
        <p>
          This workspace exposes a server-configured knowledge search tool.
          Document ingestion, index statistics and persistent agent memory are
          not connected to this console.
        </p>
      </div>
      <div className="as-mini-stats">
        <Card>
          <span className="as-stat-icon">
            <FileSearch size={22} />
          </span>
          <div>
            <strong>{configured ? "Configured" : "Not configured"}</strong>
            <span>Internal knowledge search</span>
          </div>
        </Card>
        <Card>
          <span className="as-stat-icon as-mint">
            <Database size={22} />
          </span>
          <div>
            <strong>Unavailable</strong>
            <span>Index and document statistics</span>
          </div>
        </Card>
        <Card>
          <span className="as-stat-icon as-amber">
            <HardDrive size={22} />
          </span>
          <div>
            <strong>Not connected</strong>
            <span>Persistent agent memory</span>
          </div>
        </Card>
      </div>
      <div className="as-settings-grid">
        <Card>
          <div className="as-card-heading">
            <div className="as-card-title">
              <BookOpen size={22} />
              <h2>Internal knowledge search</h2>
            </div>
            <Badge status={configured ? "configured" : "pending"}>
              {configured ? "Configured" : "Not configured"}
            </Badge>
          </div>
          <p className="as-copy">
            The registered <code>search_knowledge_base</code> tool sends a
            bounded query to your organization’s search service. Access is
            granted per agent through its tool allowlist.
          </p>
          <div
            className="as-connection-diagram"
            aria-label="An agent calls the allowlisted search tool, which queries the configured knowledge endpoint"
          >
            <span>
              <BookOpen size={20} />
              Agent
            </span>
            <span aria-hidden="true">→</span>
            <span>
              <ShieldCheck size={20} />
              Allowed tool
            </span>
            <span aria-hidden="true">→</span>
            <span>
              <Database size={20} />
              Search service
            </span>
          </div>
          <p className="as-note">
            Configuration does not establish service health or index readiness.
            Search behavior must be verified in an actual workflow.
          </p>
          <Link className="as-text-link" to="/agents">
            Manage agent tool access →
          </Link>
        </Card>
        <Card>
          <div className="as-card-title">
            <LockKeyhole size={22} />
            <h2>Controlled access</h2>
          </div>
          <ul className="as-knowledge-list">
            <li>
              <ShieldCheck size={19} />
              <div>
                <strong>Explicit tool permissions</strong>
                <p>
                  An agent can call only its selected tools. Sensitive data
                  authorization also belongs in the search service.
                </p>
              </div>
            </li>
            <li>
              <Network size={19} />
              <div>
                <strong>Operator-managed endpoint</strong>
                <p>
                  The server operator configures the trusted search service. Its
                  address and credentials are not edited in agent prompts.
                </p>
              </div>
            </li>
            <li>
              <FileSearch size={19} />
              <div>
                <strong>Traceable execution</strong>
                <p>
                  Use run events to inspect when the tool was called and how the
                  workflow continued.
                </p>
              </div>
            </li>
          </ul>
          <Link className="as-text-link" to="/runs">
            Inspect runs and traces →
          </Link>
        </Card>
      </div>
      <Card>
        <div className="as-card-title">
          <Network size={22} />
          <h2>Connect your knowledge service</h2>
        </div>
        <div className="as-setup-steps">
          <div>
            <span>1</span>
            <h3>Configure the service</h3>
            <p>
              The operator sets the internal search endpoint and the required
              backend access policy.
            </p>
          </div>
          <div>
            <span>2</span>
            <h3>Grant agent access</h3>
            <p>
              Select the search tool on an agent using a runtime that supports
              tool calling.
            </p>
          </div>
          <div>
            <span>3</span>
            <h3>Verify a workflow</h3>
            <p>
              Run a representative query and inspect the real search result,
              tool event and final response.
            </p>
          </div>
        </div>
        <p className="as-footnote">
          Upload, indexing, connectors and memory management will need a
          dedicated backend integration before they can appear as working
          controls.
        </p>
      </Card>
    </div>
  );
}
