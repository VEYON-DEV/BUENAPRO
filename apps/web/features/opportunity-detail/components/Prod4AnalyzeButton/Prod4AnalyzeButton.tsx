"use client";
import { useEffect, useId, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { prod4AnalysisRequest } from "../../api/prod4Analysis";
import styles from "./Prod4AnalyzeButton.module.css";

export function Prod4AnalyzeButton({ id, evaluated, ready }: { id: string; evaluated: boolean; ready: boolean }) {
  const router = useRouter();
  const descriptionId = useId();
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const controller = useRef<AbortController | null>(null);
  const locked = useRef(false);
  useEffect(() => () => controller.current?.abort(), []);
  async function analyze() {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    const abort = new AbortController();
    controller.current = abort;
    try {
      const job = jobId ? { job_id: jobId } : await prod4AnalysisRequest(id, { force: evaluated, signal: abort.signal });
      setJobId(job.job_id);
      const until = Date.now() + 120_000;
      while (!abort.signal.aborted) {
        const result = await prod4AnalysisRequest(id, { jobId: job.job_id, signal: abort.signal });
        if (result.status === "done") {
          setJobId(null);
          setMessage("Análisis completado. Actualizando resultados…");
          router.refresh();
          return;
        }
        if (result.status === "dead" || result.status === "failed" || result.status === "skipped") {
          setJobId(null);
          throw new Error(result.error || "El análisis no se completó. Intenta nuevamente.");
        }
        if (Date.now() >= until) {
          setMessage("El análisis sigue en cola. Consulta su estado sin generar otra evaluación.");
          return;
        }
        setMessage(result.status === "claimed" ? "La IA está evaluando tu perfil. El resultado se guardará automáticamente." : "Análisis en cola. El resultado se guardará automáticamente.");
        await new Promise<void>((resolve) => {
          const finish = () => { clearTimeout(timer); abort.signal.removeEventListener("abort", finish); resolve(); };
          const timer = setTimeout(finish, 3000);
          abort.signal.addEventListener("abort", finish, { once: true });
        });
      }
    } catch (error) {
      if (!abort.signal.aborted) setMessage(error instanceof Error ? error.message : "No se pudo analizar. Intenta nuevamente.");
    } finally {
      locked.current = false;
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  return <div className={styles.action}>
    <Button onClick={analyze} disabled={busy || !ready} aria-busy={busy} aria-describedby={descriptionId}>
      <Sparkles size={16} aria-hidden="true" />
      {busy ? "Analizando con IA…" : jobId ? "Consultar análisis" : evaluated ? "Volver a analizar con IA" : "Analizar con IA"}
    </Button>
    <p id={descriptionId} className={styles.feedback} role="status" aria-live="polite">{!ready ? "Primero deben procesarse y validarse las bases oficiales." : message || "Evalúa requisitos, brechas y puntaje con tu perfil."}</p>
  </div>;
}
