import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { getProd2OpportunityForTenant, Prod2RequestError } from "@/server/services/prod2Opportunities";
import { Prod2DetailContent } from "../../components/Prod2DetailContent";
import styles from "./Prod2OpportunityDetailPage.module.css";

export async function Prod2OpportunityDetailPage({ tenantId, idConvocatoria, profileId }: { tenantId: string; idConvocatoria: string; profileId: string | null }) {
  try {
    const detail = await getProd2OpportunityForTenant(tenantId, idConvocatoria, profileId);
    return <AppShell title="Detalle"><div className={styles.page}>{detail ? <Prod2DetailContent detail={detail} />
      : <EmptyState title="Procedimiento no disponible" action={{ label: "Volver a oportunidades", href: "/feed?source=prod2" }}>Vuelve al listado para consultar las fichas disponibles.</EmptyState>}</div></AppShell>;
  } catch (error) {
    return <AppShell title="Detalle"><EmptyState title={error instanceof Prod2RequestError ? "Perfil no disponible" : "No se pudo cargar el procedimiento"}
      action={{ label: "Volver a oportunidades", href: "/feed?source=prod2" }}>Vuelve al listado e intenta abrir la ficha otra vez.</EmptyState></AppShell>;
  }
}
