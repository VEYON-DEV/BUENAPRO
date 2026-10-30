import type { Metadata } from "next";
import { OpsItemsPage } from "@/features/ops";
import { toUrlSearchParams } from "@/lib/api/searchParams";
import { currentOpsAdminId, requireOpsAdmin } from "@/server/auth/ops";

export const dynamic = "force-dynamic";

export async function generateMetadata(): Promise<Metadata> {
  if (!(await currentOpsAdminId())) return {};
  return { title: "Elementos · Operación · BuenaPro", robots: { index: false, follow: false } };
}

export default async function OpsItemsRoute({ searchParams }: { searchParams?: Promise<Record<string, string | string[] | undefined>> }) {
  await requireOpsAdmin();
  return <OpsItemsPage params={await toUrlSearchParams(searchParams)} />;
}
