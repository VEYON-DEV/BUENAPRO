"use client";

import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  ArrowUp,
  BookOpenText,
  Check,
  ChevronDown,
  CircleAlert,
  FileCheck2,
  MessageCircleQuestion,
  Sparkles,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import {
  decideChangeSet,
  getCopilotSession,
  openCopilotSession,
  sendCopilotMessage,
} from "../../api/copilot";
import type {
  CopilotChangeSet,
  CopilotMessage,
  CopilotSession,
} from "../../model/types";
import styles from "./CopilotPanel.module.css";

const STARTERS = [
  {
    icon: CircleAlert,
    label: "Requisitos críticos",
    prompt: "¿Qué requisitos son críticos?",
  },
  {
    icon: Check,
    label: "Validar mi perfil",
    prompt: "¿Mi empresa puede cumplir?",
  },
  {
    icon: FileCheck2,
    label: "Preparar borrador",
    prompt: "Ayúdame a completar el borrador",
  },
];

function changeLabels(changes: Record<string, unknown>) {
  const labels: string[] = [];
  if (Array.isArray(changes.items) && changes.items.length)
    labels.push("Ítems y precios");
  if (Array.isArray(changes.requirements) && changes.requirements.length)
    labels.push("Respuestas RTM");
  const application =
    changes.application && typeof changes.application === "object"
      ? (changes.application as Record<string, unknown>)
      : {};
  if (application.validity_date) labels.push("Vigencia");
  if (application.contact_email || application.contact_phone)
    labels.push("Datos de contacto");
  return labels.length
    ? labels
    : Object.keys(changes).map((key) => key.replaceAll("_", " "));
}

function Message({
  message,
  working,
  onDecision,
}: {
  message: CopilotMessage;
  working: boolean;
  onDecision: (
    changeSet: CopilotChangeSet,
    decision: "confirm" | "reject",
  ) => void;
}) {
  const assistant = message.role !== "user";

  return (
    <article className={styles.message} data-role={message.role}>
      {assistant ? (
        <div className={styles.assistantMark} aria-hidden="true">
          <Sparkles size={15} strokeWidth={2} />
        </div>
      ) : null}
      <div className={styles.messageContent}>
        <span className={styles.messageAuthor}>
          {assistant ? "BuenaPro" : "Tú"}
        </span>
        <div className={styles.bubble}>
          <div className={styles.markdown}>
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
          {message.citations.length ? (
            <details className={styles.sources}>
              <summary>
                <BookOpenText size={14} aria-hidden="true" />
                {message.citations.length} fuentes consultadas
                <ChevronDown
                  className={styles.sourceChevron}
                  size={14}
                  aria-hidden="true"
                />
              </summary>
              <ul>
                {message.citations.map((citation, index) => (
                  <li key={`${citation.label}-${index}`}>
                    <strong>{citation.label}</strong>
                    {citation.excerpt ? <span>{citation.excerpt}</span> : null}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
        {message.changeSet ? (
          <section className={styles.changeSet} aria-label="Cambios sugeridos">
            <div className={styles.changeHeading}>
              <span className={styles.changeIcon} aria-hidden="true">
                <FileCheck2 size={16} />
              </span>
              <div>
                <strong>
                  {message.changeSet.summary || "Cambios listos para revisar"}
                </strong>
                <span>Tu borrador no cambia hasta que lo confirmes.</span>
              </div>
            </div>
            <ul>
              {changeLabels(message.changeSet.changes).map((label) => (
                <li key={label}>{label}</li>
              ))}
            </ul>
            {message.changeSet.status === "pending" ? (
              <div className={styles.changeActions}>
                <button
                  disabled={working}
                  type="button"
                  onClick={() => onDecision(message.changeSet!, "confirm")}
                >
                  Aplicar al borrador
                </button>
                <button
                  disabled={working}
                  type="button"
                  onClick={() => onDecision(message.changeSet!, "reject")}
                >
                  Descartar
                </button>
              </div>
            ) : (
              <span className={styles.decisionState}>
                {message.changeSet.status === "applied"
                  ? "Cambios aplicados manualmente"
                  : "Sugerencia descartada"}
              </span>
            )}
          </section>
        ) : null}
      </div>
    </article>
  );
}

export function CopilotPanel({
  contractId,
  matchId,
  onApplied,
}: {
  contractId: number;
  matchId?: string;
  onApplied?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [session, setSession] = useState<CopilotSession | null>(null);
  const [draft, setDraft] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open || session || working) return;
    setWorking(true);
    openCopilotSession(contractId, matchId)
      .then(setSession)
      .catch((cause) =>
        setError(
          cause instanceof Error
            ? cause.message
            : "No se pudo abrir el copiloto.",
        ),
      )
      .finally(() => setWorking(false));
  }, [contractId, matchId, open, session, working]);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "auto", block: "nearest" });
  }, [session?.messages.length, working]);

  useEffect(() => {
    if (!open) return;
    const focusTimer = window.setTimeout(() => {
      if (window.matchMedia("(min-width: 641px)").matches) {
        textarea.current?.focus();
      }
    }, 180);
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        launcher.current?.focus();
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  async function submit(event?: FormEvent, preset?: string) {
    event?.preventDefault();
    const content = (preset ?? draft).trim();
    if (!content || !session || working) return;
    setDraft("");
    setError("");
    setWorking(true);
    try {
      const result = await sendCopilotMessage(session.id, content);
      setSession((current) =>
        current
          ? {
              ...current,
              messages: [
                ...current.messages,
                result.userMessage,
                result.assistantMessage,
              ],
            }
          : current,
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "No se pudo responder.",
      );
    } finally {
      setWorking(false);
    }
  }

  async function decide(
    changeSet: CopilotChangeSet,
    decision: "confirm" | "reject",
  ) {
    if (!session || working) return;
    setWorking(true);
    setError("");
    try {
      await decideChangeSet(changeSet.id, decision);
      const refreshed = await getCopilotSession(session.id);
      setSession(refreshed);
      if (decision === "confirm") onApplied?.();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "No se pudo procesar la decisión.",
      );
    } finally {
      setWorking(false);
    }
  }

  function closePanel() {
    setOpen(false);
    window.setTimeout(() => launcher.current?.focus(), 0);
  }

  function keepFocusInside(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key !== "Tab") return;
    const focusable = panel.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable?.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <>
      <button
        ref={launcher}
        className={styles.launcher}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={`copilot-panel-${contractId}`}
      >
        <span className={styles.launcherIcon} aria-hidden="true">
          <Sparkles size={18} strokeWidth={2} />
        </span>
        <span className={styles.launcherCopy}>
          <strong>Preguntar a BuenaPro</strong>
          <small>Analiza esta oportunidad</small>
        </span>
      </button>
      {open ? (
        <button
          className={styles.backdrop}
          type="button"
          onClick={closePanel}
          aria-hidden="true"
          tabIndex={-1}
        />
      ) : null}
      <aside
        ref={panel}
        id={`copilot-panel-${contractId}`}
        className={styles.panel}
        data-open={open}
        role="dialog"
        aria-modal="true"
        aria-hidden={!open}
        aria-labelledby={`copilot-title-${contractId}`}
        onKeyDown={keepFocusInside}
      >
        <header className={styles.header}>
          <div className={styles.headerIdentity}>
            <span className={styles.headerIcon} aria-hidden="true">
              <Sparkles size={20} strokeWidth={2} />
            </span>
            <div>
              <div className={styles.availability}>
                <i /> Asistente contextual
              </div>
              <strong id={`copilot-title-${contractId}`}>
                Pregunta a BuenaPro
              </strong>
            </div>
          </div>
          <button
            className={styles.closeButton}
            type="button"
            onClick={closePanel}
            aria-label="Cerrar asistente"
            title="Cerrar"
          >
            <X size={20} aria-hidden="true" />
          </button>
        </header>

        <div className={styles.contextBar}>
          <MessageCircleQuestion size={16} aria-hidden="true" />
          <div>
            <strong>Oportunidad #{contractId}</strong>
            <span>Analiza el TDR, tu perfil y el borrador actual</span>
          </div>
        </div>

        <div className={styles.thread} aria-live="polite" aria-busy={working}>
          {!session?.messages.length && !working ? (
            <div className={styles.empty}>
              <span className={styles.emptyIcon} aria-hidden="true">
                <Sparkles size={24} />
              </span>
              <strong>¿Qué quieres revisar?</strong>
              <p>
                Puedo ayudarte a entender los requisitos, detectar brechas y
                preparar cambios. Tú confirmas antes de modificar el borrador.
              </p>
            </div>
          ) : null}

          {!session?.messages.length ? (
            <div className={styles.starters} aria-label="Preguntas sugeridas">
              <span>Preguntas sugeridas</span>
              <div>
                {STARTERS.map(({ icon: Icon, label, prompt }) => (
                  <button
                    key={prompt}
                    type="button"
                    onClick={() => void submit(undefined, prompt)}
                    disabled={!session || working}
                  >
                    <Icon size={15} aria-hidden="true" />
                    {label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {session?.messages.map((message) => (
            <Message
              key={message.id}
              message={message}
              working={working}
              onDecision={(set, decision) => void decide(set, decision)}
            />
          ))}
          {working ? (
            <div className={styles.thinking} role="status">
              <span />
              <span />
              <span />
              <span className={styles.thinkingLabel}>Analizando expediente</span>
            </div>
          ) : null}
          {error ? (
            <div className={styles.error} role="alert">
              <CircleAlert size={16} aria-hidden="true" />
              {error}
            </div>
          ) : null}
          <div ref={end} />
        </div>

        <form className={styles.composer} onSubmit={(event) => void submit(event)}>
          <label className={styles.srOnly} htmlFor={`copilot-${contractId}`}>
            Mensaje para el asistente de BuenaPro
          </label>
          <div className={styles.composerField}>
            <textarea
              ref={textarea}
              id={`copilot-${contractId}`}
              name="copilot-message"
              autoComplete="off"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submit();
                }
              }}
              placeholder="Pregunta sobre esta oportunidad…"
              rows={2}
            />
            <button
              type="submit"
              disabled={!draft.trim() || !session || working}
              aria-label="Enviar mensaje"
              title="Enviar mensaje"
            >
              <ArrowUp size={19} aria-hidden="true" />
            </button>
          </div>
          <div className={styles.composerMeta}>
            <span>La IA puede equivocarse. Verifica la información oficial.</span>
            <span className={styles.keyboardHint}>Enter envía · Shift + Enter añade línea</span>
          </div>
        </form>
      </aside>
    </>
  );
}
