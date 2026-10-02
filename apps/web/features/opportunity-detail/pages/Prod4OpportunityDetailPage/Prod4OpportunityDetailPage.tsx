import { AppShell } from "@/features/shell";
import { EmptyState } from "@/components/ui/EmptyState";
import { getProd4OpportunityForTenant } from "@/server/services/prod4Opportunities";
import { Prod4DetailContent } from "../../components/Prod4DetailContent";
import styles from "./Prod4OpportunityDetailPage.module.css";

export async function Prod4OpportunityDetailPage({ tenantId, idProcedimiento }: { tenantId:string; idProcedimiento:string }) {
  const detail = await getProd4OpportunityForTenant(tenantId, idProcedimiento);
  return <AppShell title="Detalle"><div className={styles.page}>{detail ? <Prod4DetailContent detail={detail} /> : <EmptyState title="Concurso no encontrado" action={{ label:"Volver a concursos SEACE", href:"/feed?source=prod4" }}>El concurso no está disponible en el alcance de tecnología.</EmptyState>}</div></AppShell>;
}
