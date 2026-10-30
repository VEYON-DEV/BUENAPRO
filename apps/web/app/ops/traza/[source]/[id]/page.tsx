import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { OpsTracePage } from "@/features/ops";
import { currentOpsAdminId, requireOpsAdmin } from "@/server/auth/ops";
import { isTraceId, parseSource } from "@/server/services/ops/filters";

export const dynamic = "force-dynamic";

export async function generateMetadata(): Promise<Metadata> {
  if (!(await currentOpsAdminId())) return {};
  return { title: "Traza · Operación · BuenaPro", robots: { index: false, follow: false } };
}

export default async function OpsTraceRoute({ params }: { params: Promise<{ source: string; id: string }> }) {
  await requireOpsAdmin();
  const { source: rawSource, id } = await params;
  const source = parseSource(rawSource);
  if (!source || !isTraceId(id)) notFound();
  return <OpsTracePage source={source} id={id} />;
}
