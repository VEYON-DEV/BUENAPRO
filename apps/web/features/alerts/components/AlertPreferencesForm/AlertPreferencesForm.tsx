"use client";

import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Switch } from "@/components/ui/Switch";
import { apiFetch } from "@/lib/api/client";
import styles from "./AlertPreferencesForm.module.css";

type Preference = { enabled: boolean; min_verdict: string; max_alerts_per_day: number };

export function AlertPreferencesForm({ preference }: { preference: Preference }) {
  const [status, setStatus] = useState("");
  const [enabled, setEnabled] = useState(Boolean(preference.enabled));

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setStatus("Guardando…");
    const form = new FormData(event.currentTarget);
    try {
      await apiFetch("/api/notifications/prefs", {
        method: "PUT",
        json: {
          channel: "in_app",
          enabled,
          min_verdict: form.get("min_verdict"),
          max_alerts_per_day: Number(form.get("max_alerts_per_day")),
          mode: "realtime",
        },
      });
      setStatus("Preferencias guardadas");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "No se pudo guardar");
    }
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.heading}>
        <h2>Qué debe avisarte BuenaPro</h2>
        <p>Controla la exigencia del radar y el límite diario.</p>
      </div>
      <Switch checked={enabled} label="Alertas activas" onChange={(event) => setEnabled(event.target.checked)} />
      <label><span>Nivel mínimo</span><Select defaultValue={preference.min_verdict} name="min_verdict"><option value="verde">Solo afinidad alta</option><option value="ambar">Alta y posible</option></Select></label>
      <label><span>Máximo diario</span><Select defaultValue={String(preference.max_alerts_per_day ?? 0)} name="max_alerts_per_day"><option value="0">Sin límite</option><option value="5">5 alertas</option><option value="10">10 alertas</option><option value="25">25 alertas</option></Select></label>
      <Button type="submit">Guardar alertas</Button>
      <span className={styles.status} aria-live="polite">{status}</span>
    </form>
  );
}
