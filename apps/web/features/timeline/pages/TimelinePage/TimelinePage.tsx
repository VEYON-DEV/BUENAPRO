import { AppShell } from "@/features/shell";
import { PageHeader } from "@/components/ui/PageHeader";
import { TimelineBoard } from "../../components/TimelineBoard";
import type { TimelineResponse } from "@/lib/procurementTimeline";
import styles from "./TimelinePage.module.css";

export function TimelinePage({ initial, params }: { initial: TimelineResponse; params: URLSearchParams }) {
  return <AppShell><div className={styles.page}>
    <PageHeader title="Cronograma" description="Tus oportunidades con potencial, en el tiempo. Anticípate a las preguntas y al cierre de ofertas." />
    <TimelineBoard initial={initial} initialParams={Object.fromEntries(params)} />
  </div></AppShell>;
}
