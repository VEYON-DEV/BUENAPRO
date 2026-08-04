import { redirect } from "next/navigation";
import { ProfilePage } from "@/features/profile";
import { currentTenantId } from "@/server/auth/tenant";

export default async function PerfilRoute() {
  const tenantId = await currentTenantId();
  if (!tenantId) redirect("/login");
  return <ProfilePage tenantId={tenantId} />;
}
