"use client";

import { useEffect, useState } from "react";
import { Gauge, Radar, Save, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Switch } from "@/components/ui/Switch";
import { getAutomationRules, updateAutomationRules } from "../../api/settings";
import type { AutomationRules } from "../../model/types";
import styles from "./AutomationSettingsPanel.module.css";

const defaults: AutomationRules = {
  enabled: true,
  min_fit_level: 2,
  min_notification_score: 50,
  max_daily_evaluations: 0,
  min_hours_before_close: 0,
};

export function AutomationSettingsPanel() {
  const [rules, setRules] = useState<AutomationRules>(defaults);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    getAutomationRules()
      .then((data) => {
        if (active) setRules(data);
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : "No se pudo cargar el radar");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  function update<K extends keyof AutomationRules>(key: K, value: AutomationRules[K]) {
    setRules((current) => ({ ...current, [key]: value }));
    setMessage("");
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const saved = await updateAutomationRules(rules);
      setRules({ ...rules, ...saved });
      setMessage("Configuración del radar guardada");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo guardar el radar");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className={styles.panel} onSubmit={save} aria-labelledby="automation-title">
      <header className={styles.header}>
        <span className={styles.icon} aria-hidden="true"><Radar size={20} /></span>
        <div>
          <h2 id="automation-title">Radar automático</h2>
          <p>Decide qué oportunidades pasan del TDR a la evaluación completa.</p>
        </div>
        <Switch
          checked={rules.enabled}
          disabled={loading}
          label={rules.enabled ? "Activo" : "Pausado"}
          onChange={(event) => update("enabled", event.target.checked)}
        />
      </header>

      <div className={styles.body} aria-busy={loading}>
        <fieldset className={styles.fitFieldset} disabled={loading}>
          <legend>Afinidad mínima para evaluar</legend>
          <p>La IA se ejecuta después de procesar el TDR y alcanzar este nivel preliminar.</p>
          <div className={styles.fitOptions}>
            {[1, 2, 3].map((level) => (
              <label className={rules.min_fit_level === level ? styles.fitActive : ""} key={level}>
                <input
                  checked={rules.min_fit_level === level}
                  name="min_fit_level"
                  onChange={() => update("min_fit_level", level)}
                  type="radio"
                  value={level}
                />
                <span className={styles.dots} aria-hidden="true">
                  {[1, 2, 3].map((dot) => <i className={dot <= level ? styles.dotOn : ""} key={dot} />)}
                </span>
                <strong>{level === 1 ? "General" : level === 2 ? "Relacionado" : "Rubro exacto"}</strong>
                <small>{level === 2 ? "Recomendado" : level === 1 ? "Más amplio" : "Más estricto"}</small>
              </label>
            ))}
          </div>
        </fieldset>

        <div className={styles.ruleGrid}>
          <label>
            <span><Gauge size={16} /> Puntaje para notificar</span>
            <Input
              disabled={loading}
              autoComplete="off"
              inputMode="numeric"
              max={99}
              min={0}
              name="min_notification_score"
              onChange={(event) => update("min_notification_score", Number(event.target.value))}
              type="number"
              value={rules.min_notification_score}
            />
            <small>Se avisarán resultados mayores a este valor.</small>
          </label>
          <label>
            <span><Sparkles size={16} /> Evaluaciones diarias</span>
            <Input
              disabled={loading}
              autoComplete="off"
              inputMode="numeric"
              max={200}
              min={0}
              name="max_daily_evaluations"
              onChange={(event) => update("max_daily_evaluations", Number(event.target.value))}
              type="number"
              value={rules.max_daily_evaluations}
            />
            <small>Usa 0 para no aplicar un límite diario.</small>
          </label>
          <label>
            <span>Tiempo mínimo antes del cierre</span>
            <Select
              disabled={loading}
              name="min_hours_before_close"
              onChange={(event) => update("min_hours_before_close", Number(event.target.value))}
              value={rules.min_hours_before_close}
            >
              {![0, 2, 4, 8, 24].includes(rules.min_hours_before_close) ? (
                <option value={rules.min_hours_before_close}>{rules.min_hours_before_close} horas</option>
              ) : null}
              <option value={0}>Evaluar todas</option>
              <option value={2}>Al menos 2 horas</option>
              <option value={4}>Al menos 4 horas</option>
              <option value={8}>Al menos 8 horas</option>
              <option value={24}>Al menos 24 horas</option>
            </Select>
            <small>Evita gastar una evaluación cuando ya no queda tiempo para actuar.</small>
          </label>
        </div>
      </div>

      <footer className={styles.footer}>
        <span className={error ? styles.error : styles.status} role={error ? "alert" : "status"} aria-live="polite">
          {error || message}
        </span>
        <Button disabled={loading || saving} type="submit"><Save size={16} /> {saving ? "Guardando…" : "Guardar radar"}</Button>
      </footer>
    </form>
  );
}
