import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
} from "react";
import {
  Activity,
  AlertCircle,
  LoaderCircle,
  X,
  type LucideIcon,
} from "lucide-react";

export function Button({
  children,
  variant = "secondary",
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
}) {
  return (
    <button
      type={type}
      className={`button button-${variant} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
export function Card({
  children,
  className = "",
  ...props
}: HTMLAttributes<HTMLElement>) {
  return (
    <section className={`card ${className}`} {...props}>
      {children}
    </section>
  );
}
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
export function Badge({
  status,
  children,
}: {
  status: string;
  children?: ReactNode;
}) {
  return (
    <span
      className={`badge badge-${status.toLowerCase().replaceAll(/[^a-z_]/g, "")}`}
    >
      <span className="status-dot" />
      {children ??
        status.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase())}
    </span>
  );
}
export function Spinner() {
  return (
    <span className="spinner" role="status" aria-label="Loading">
      <LoaderCircle size={20} />
    </span>
  );
}
export function ErrorNotice({ message }: { message?: string | null }) {
  return message ? (
    <div className="error-notice" role="alert">
      <AlertCircle size={17} />
      <span>{message}</span>
    </div>
  ) : null;
}
export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Activity,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  icon?: LucideIcon;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon size={25} />
      </span>
      <h3>{title}</h3>
      {description && <p>{description}</p>}
      {action}
    </div>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: ReactNode;
  children: ReactNode;
  hint?: ReactNode;
}) {
  const generated = useId();
  const child = isValidElement<Record<string, unknown>>(children)
    ? children
    : null;
  const native =
    child &&
    typeof child.type === "string" &&
    ["input", "select", "textarea"].includes(child.type);
  const id =
    native && typeof child.props.id === "string" ? child.props.id : generated;
  const described = [
    native && typeof child.props["aria-describedby"] === "string"
      ? child.props["aria-describedby"]
      : "",
    hint ? `${id}-hint` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="field">
      {native ? (
        <label className="field-label" htmlFor={id}>
          {label}
        </label>
      ) : (
        <span className="field-label" id={`${id}-label`}>
          {label}
        </span>
      )}
      {native ? (
        cloneElement(child, {
          id,
          ...(described ? { "aria-describedby": described } : {}),
        })
      ) : (
        <div role="group" aria-labelledby={`${id}-label`}>
          {children}
        </div>
      )}
      {hint && (
        <span className="field-hint" id={`${id}-hint`}>
          {hint}
        </span>
      )}
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = () =>
      Array.from(
        ref.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex="0"]',
        ) ?? [],
      );
    const initial = ref.current?.querySelector<HTMLElement>(
      'input:not([type="checkbox"]):not([disabled]), textarea:not([disabled]), select:not([disabled])',
    );
    (initial ?? focusable()[0])?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close.current();
      if (event.key === "Tab") {
        const items = focusable(),
          first = items[0],
          last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", keydown);
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className={`modal ${wide ? "modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <Button variant="ghost" aria-label="Close dialog" onClick={onClose}>
            <X size={19} />
          </Button>
        </div>
        {children}
      </div>
    </div>
  );
}
