import { redirect } from "next/navigation";
import { toUrlSearchParams } from "@/lib/api/searchParams";
import { MarketPage } from "@/features/market-intelligence/MarketPage";
import { currentTenantId } from "@/server/auth/tenant";

export default async function MarketRoute({
  searchParams,
}: {
  searchParams?: Promise<Record<string, string | string[] | undefined>>;
}) {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  return <MarketPage tenantId={tenantId} params={await toUrlSearchParams(searchParams)} />;
}
