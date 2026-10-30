import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { majorGradingFacts } from "@/server/services/manualGrading";
import { getProd2OpportunityForTenant, Prod2RequestError } from "@/server/services/prod2Opportunities";
import { Prod2DetailContent } from "../../components/Prod2DetailContent";
import { canOfferMajorGrading } from "../../model/manualGrading";
import styles from "./Prod2OpportunityDetailPage.module.css";

/** ¿Sigue abierta? Mismo cierre SQL que usa el listado y la calificación manual; ante un fallo no se ofrece calificar. */
async function convocatoriaIsOpen(idConvocatoria: string, profileId: string | null | undefined) {
  try {
    const facts = await majorGradingFacts({ idConvocatoria, profileId: profileId ?? null });
    return facts != null && !facts.closed;
  } catch {
    return false;
  }
}

export async function Prod2OpportunityDetailPage({ tenantId, idConvocatoria, profileId }: { tenantId: string; idConvocatoria: string; profileId: string | null }) {
  try {
    const detail = await getProd2OpportunityForTenant(tenantId, idConvocatoria, profileId);
    // Solo se consulta el cierre cuando lo demás ya permite ofrecer «Calificar»: una convocatoria evaluada no lo necesita.
    const couldOffer = detail != null && canOfferMajorGrading({
      hasEvaluation: Boolean(detail.analysis.match) || Boolean(detail.process.match_id), open: true, jevScore: detail.process.jev_score,
    });
    const convocatoriaOpen = couldOffer ? await convocatoriaIsOpen(idConvocatoria, detail.profile?.id) : false;
    return <AppShell title="Detalle"><div className={styles.page}>{detail ? <Prod2DetailContent detail={detail} convocatoriaOpen={convocatoriaOpen} />
      : <EmptyState title="Procedimiento no disponible" action={{ label: "Volver a oportunidades", href: "/feed?source=prod2" }}>Vuelve al listado para consultar las fichas disponibles.</EmptyState>}</div></AppShell>;
  } catch (error) {
    return <AppShell title="Detalle"><EmptyState title={error instanceof Prod2RequestError ? "Perfil no disponible" : "No se pudo cargar el procedimiento"}
      action={{ label: "Volver a oportunidades", href: "/feed?source=prod2" }}>Vuelve al listado e intenta abrir la ficha otra vez.</EmptyState></AppShell>;
  }
}
