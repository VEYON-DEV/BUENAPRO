import type { ApplicationData } from "../../model/types";
import { AssigneeControl } from "../AssigneeControl";
import styles from "./ProgressRail.module.css";

export function ProgressRail({
  data,
  saving,
}: {
  data: ApplicationData;
  saving: boolean;
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
      <div className={styles.notice}>
        <strong>Envío oficial no habilitado</strong>
        <p>
          Tu borrador queda listo aquí. La presentación en SEACE seguirá bajo
          control del usuario hasta validar el bridge de envío.
        </p>
      </div>
    </aside>
  );
}
