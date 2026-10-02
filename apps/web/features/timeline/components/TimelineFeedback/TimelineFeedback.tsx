"use client";
import { AppShell } from "@/features/shell";
import { PageHeader } from "@/components/ui/PageHeader";
import { Button } from "@/components/ui/Button";
import styles from "./TimelineFeedback.module.css";

export function TimelineFeedback({ retry }: { retry?: () => void }) {
  return <AppShell><PageHeader title="Cronograma" /><section className={styles.message} aria-live="polite">
    <h2>{retry ? "No se pudo cargar el cronograma" : "Cargando oportunidades…"}</h2>
    <p>{retry ? "Intenta nuevamente. Tus fechas y evaluaciones guardadas no se han modificado." : "Consultando las etapas oficiales y los próximos cierres."}</p>
    {retry ? <Button onClick={retry}>Volver a intentar</Button> : null}
  </section></AppShell>;
}
