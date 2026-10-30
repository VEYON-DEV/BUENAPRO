import type { Metadata } from "next";
import { OpsPage } from "@/features/ops";
import { toUrlSearchParams } from "@/lib/api/searchParams";
import { currentOpsAdminId, requireOpsAdmin } from "@/server/auth/ops";

export const dynamic = "force-dynamic";

// Sin título ni «noindex» para quien no es administrador: la respuesta debe ser la del 404 genérico.
export async function generateMetadata(): Promise<Metadata> {
  if (!(await currentOpsAdminId())) return {};
  return { title: "Operación · BuenaPro", robots: { index: false, follow: false } };
}

export default async function OpsRoute({ searchParams }: { searchParams?: Promise<Record<string, string | string[] | undefined>> }) {
  await requireOpsAdmin();
  return <OpsPage params={await toUrlSearchParams(searchParams)} />;
}
