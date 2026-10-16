import { redirect } from "next/navigation";
import { OpportunityDetailPage } from "@/features/opportunity-detail";
import { currentTenantId } from "@/server/auth/tenant";
import { toUrlSearchParams } from "@/lib/api/searchParams";

export default async function OpportunityRoute({ params, searchParams }: { params: Promise<{ id: string }>; searchParams?: Promise<Record<string, string | string[] | undefined>> }) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const { id } = await params;
  const query = await toUrlSearchParams(searchParams);
  return <OpportunityDetailPage tenantId={tenantId} idContrato={Number(id)} profileId={query.get("profile_id")} />;
}
