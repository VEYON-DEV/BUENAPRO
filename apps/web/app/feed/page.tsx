import { redirect } from "next/navigation";
import { OpportunitiesPage } from "@/features/opportunities";
import { currentTenantId } from "@/server/auth/tenant";
import { toUrlSearchParams } from "@/lib/api/searchParams";

export default async function FeedPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const params = await toUrlSearchParams(searchParams);
  return <OpportunitiesPage tenantId={tenantId} params={params} />;
}
