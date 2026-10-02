import { redirect } from "next/navigation";
import { TimelinePage } from "@/features/timeline";
import { currentTenantId } from "@/server/auth/tenant";
import { toUrlSearchParams } from "@/lib/api/searchParams";
import { getProcurementTimeline, parseTimelineParams } from "@/server/services/procurementTimeline";

export default async function Page({ searchParams }: { searchParams?: Promise<Record<string, string | string[] | undefined>> }) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  const params = await toUrlSearchParams(searchParams);
  const initial = await getProcurementTimeline(tenantId, parseTimelineParams(params));
  return <TimelinePage initial={initial} params={params} />;
}
