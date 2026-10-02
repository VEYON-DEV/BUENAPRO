import { notFound, redirect } from "next/navigation";
import { currentTenantId } from "@/server/auth/tenant";
import { Prod4OpportunityDetailPage } from "@/features/opportunity-detail";

export default async function Prod4DetailRoute({ params }: { params:Promise<{id:string}> }) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const { id } = await params;
  if (!/^[1-9]\d{0,18}$/.test(id)) notFound();
  return <Prod4OpportunityDetailPage tenantId={tenantId} idProcedimiento={id} />;
}
