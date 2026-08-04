import { redirect } from "next/navigation";
import { SettingsPage } from "@/features/alerts";
import { currentTenantId } from "@/server/auth/tenant";

export default async function ConfiguracionRoute() {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  return <SettingsPage />;
}
