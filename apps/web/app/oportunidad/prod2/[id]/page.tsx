import { notFound, redirect } from "next/navigation";
import { currentTenantId } from "@/server/auth/tenant";
import { Prod2OpportunityDetailPage } from "@/features/opportunity-detail";
import { toUrlSearchParams } from "@/lib/api/searchParams";

export default async function Prod2DetailRoute({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const { id } = await params;
  if (!/^[1-9]\d{0,18}$/.test(id)) notFound();
  const query = await toUrlSearchParams(searchParams);
  return <Prod2OpportunityDetailPage tenantId={tenantId} idConvocatoria={id} profileId={query.get("profile_id")} />;
}
