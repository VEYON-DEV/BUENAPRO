import { redirect } from "next/navigation";
import { AlertsPage } from "@/features/alerts";
import { currentTenantId } from "@/server/auth/tenant";

export default async function AlertsRoute() {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  return <AlertsPage tenantId={tenantId} />;
}
