import Link from "next/link";
import { Bell, Settings2 } from "lucide-react";
import { PageHeader } from "@/components/ui/PageHeader";
import { AppShell } from "@/features/shell";
import { AutomationSettingsPanel } from "../../components/AutomationSettingsPanel";
import { TelegramSettingsPanel } from "../../components/TelegramSettingsPanel";
import styles from "./SettingsPage.module.css";

export function SettingsPage() {
  return (
    <AppShell title="Configuración">
      <PageHeader
        title="Configuración"
        description="Controla la evaluación automática y dónde recibes las oportunidades recomendadas."
        actions={
          <nav className={styles.viewNav} aria-label="Configuración y alertas">
            <Link className={styles.active} href="/configuracion" aria-current="page"><Settings2 size={17} /> Configuración</Link>
            <Link href="/alertas"><Bell size={17} /> Ver alertas</Link>
          </nav>
        }
      />

      <div className={styles.intro}>
        <div>
          <strong>Flujo conectado</strong>
          <span>TDR procesado → afinidad preliminar → evaluación completa → alerta.</span>
        </div>
        <span className={styles.threshold}>Umbrales configurables por empresa</span>
      </div>

      <div className={styles.grid}>
        <AutomationSettingsPanel />
        <TelegramSettingsPanel />
      </div>
    </AppShell>
  );
}
