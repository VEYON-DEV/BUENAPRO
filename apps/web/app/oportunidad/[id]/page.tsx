import { redirect } from "next/navigation";
import { OpportunityDetailPage } from "@/features/opportunity-detail";
import { currentTenantId } from "@/server/auth/tenant";

export default async function OpportunityRoute({ params }: { params: Promise<{ id: string }> }) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const { id } = await params;
  return <OpportunityDetailPage tenantId={tenantId} idContrato={Number(id)} />;
}
