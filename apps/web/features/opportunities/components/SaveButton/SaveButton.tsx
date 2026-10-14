"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Bookmark } from "lucide-react";
import { apiFetch } from "@/lib/api/client";
import styles from "./SaveButton.module.css";

type SaveButtonProps = {
  idContrato: string | number;
  /** Save endpoint for non-contract sources (e.g. Buscador SEACE). Defaults to contratos menores. */
  endpoint?: string;
  code?: string | null;
  initialSaved?: boolean;
  showLabel?: boolean;
};

export function SaveButton(props: SaveButtonProps) {
  const endpoint = props.endpoint ?? `/api/contracts/${props.idContrato}/saved`;
  // A reused inspector must not carry optimistic state or a late response
  // from the previous opportunity into the new one.
  return <SaveControl key={JSON.stringify([String(props.idContrato), endpoint])} {...props} endpoint={endpoint} />;
}

function SaveControl({
  idContrato,
  code,
  initialSaved = false,
  showLabel = false,
  endpoint,
}: SaveButtonProps) {
  const router = useRouter();
  const [saved, setSaved] = useState(initialSaved);
  const [status, setStatus] = useState<"idle" | "saving" | "error">("idle");
  const [notice, setNotice] = useState("");
  const reference = code ? ` ${code}` : "";
  const actionLabel = saved ? `Quitar${reference} de guardadas` : `Guardar${reference}`;

  useEffect(() => {
    setSaved(initialSaved);
  }, [initialSaved]);

  async function toggle() {
    const next = !saved;
    setSaved(next);
    setStatus("saving");
    setNotice("Actualizando guardadas…");
    try {
      await apiFetch(endpoint ?? `/api/contracts/${idContrato}/saved`, {
        method: next ? "PUT" : "DELETE",
      });
      setStatus("idle");
      setNotice(next ? "Oportunidad guardada." : "Oportunidad retirada de guardadas.");
      router.refresh();
    } catch {
      setSaved(!next);
      setStatus("error");
      setNotice("No se pudo actualizar. Intenta nuevamente.");
    }
  }

  return (
    <div className={styles.wrapper}>
      <button
        aria-busy={status === "saving"}
        aria-label={actionLabel}
        aria-pressed={saved}
        className={[styles.button, saved ? styles.saved : "", status === "error" ? styles.error : ""].join(" ")}
        disabled={status === "saving"}
        onClick={toggle}
        title={status === "error" ? "No se pudo actualizar. Intenta nuevamente." : actionLabel}
        type="button"
      >
        <Bookmark aria-hidden="true" size={18} strokeWidth={1.8} />
        {showLabel ? <span>{saved ? "Guardada" : "Guardar oportunidad"}</span> : null}
      </button>
      <span aria-live="polite" className={styles.status}>
        {notice}
      </span>
    </div>
  );
}
