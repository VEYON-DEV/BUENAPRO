import { CheckCircle2, Loader2, Send } from "lucide-react";
import type { ApplicationData } from "../../model/types";
import { AssigneeControl } from "../AssigneeControl";
import styles from "./ProgressRail.module.css";

export function ProgressRail({
  data,
  saving,
  submitting,
  onSubmit,
}: {
  data: ApplicationData;
  saving: boolean;
  submitting: boolean;
  onSubmit: () => void;
}) {
  const selected = data.items.filter((item) => item.selected);
  const itemDone =
    selected.length > 0 && selected.every((item) => item.unitPrice != null);
  const reqDone =
    data.requirements.length === 0 ||
    data.requirements.every((item) => item.offered.trim());
  const contactDone = Boolean(
    data.validity && data.contactEmail && data.contactPhone,
  );
  const steps = [
    { label: "Oferta y precios", done: itemDone && contactDone },
    { label: "RTM respondidos", done: reqDone },
    { label: "Propuesta adjunta", done: data.attachments.length > 0 },
  ];
  const done = steps.filter((step) => step.done).length;
  const complete = done === steps.length;
  const submitted = data.status === "submitted";
  return (
    <aside className={styles.rail} aria-label="Estado de la postulación">
      <AssigneeControl
        matchId={data.matchId}
        initialResponsibleId={data.responsibleId}
      />
      <div className={styles.heading}>
        <div>
          <strong>
            {done} de {steps.length}
          </strong>
          <span>bloques completos</span>
        </div>
        <span className={styles.saved}>
          {saving ? "Guardando…" : "Cambios guardados"}
        </span>
      </div>
      <div
        className={styles.bar}
        aria-label={`${done} de ${steps.length} completos`}
      >
        <span style={{ width: `${(done / steps.length) * 100}%` }} />
      </div>
      <ol>
        {steps.map((step) => (
          <li key={step.label} data-done={step.done}>
            <span aria-hidden="true">{step.done ? "✓" : ""}</span>
            {step.label}
          </li>
        ))}
      </ol>
      {submitted ? (
        <div className={styles.success} role="status" aria-live="polite">
          <CheckCircle2 aria-hidden="true" />
          <div>
            <strong>¡Felicidades! Tu licitación quedó registrada</strong>
            <p>
              BuenaPro guardó la postulación como presentada. Conserva el cargo
              de SEACE como respaldo.
            </p>
          </div>
        </div>
      ) : (
        <div className={styles.submitArea}>
          <button
            className={styles.submitButton}
            type="button"
            disabled={!complete || saving || submitting}
            onClick={onSubmit}
          >
            {submitting ? (
              <Loader2 className={styles.spinner} aria-hidden="true" />
            ) : (
              <Send aria-hidden="true" />
            )}
            {submitting ? "Registrando…" : "Registrar como presentada"}
          </button>
          <p>
            {complete
              ? "Úsalo después de presentar la propuesta en SEACE."
              : "Completa los tres bloques para habilitar esta acción."}
          </p>
        </div>
      )}
    </aside>
  );
}
