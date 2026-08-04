import { notFound, redirect } from "next/navigation";
import { SupplierDetailPage } from "@/features/market-intelligence";
import { currentTenantId } from "@/server/auth/tenant";
import { getHistoricalSupplier } from "@/server/services/market";

export default async function SupplierRoute({ params }: { params: Promise<{ ruc: string }> }) {
  if (!(await currentTenantId())) redirect("/login");
  const ruc = (await params).ruc.replace(/\D/g, "");
  if (ruc.length !== 11) notFound();
  const data = await getHistoricalSupplier(ruc);
  if (!data) notFound();
  return <SupplierDetailPage data={data as any} />;
}
