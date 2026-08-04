import { redirect } from "next/navigation";
import { DashboardPage } from "@/features/dashboard";
import { currentTenantId } from "@/server/auth/tenant";

export default async function HomePage() {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  return <DashboardPage tenantId={tenantId} />;
}
