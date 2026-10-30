import type { NextRequest } from "next/server";
import { opsApiGuard } from "@/server/auth/ops";
import { listOpsCompanies } from "@/server/services/ops/companies";
import { parseItemsQuery } from "@/server/services/ops/filters";
import { listItems } from "@/server/services/ops/items";
import { opsBadRequest, opsJson, opsServerError } from "../_utils";

export const dynamic = "force-dynamic";

/** La lista detrás de un número: source=menores|mayores, day=YYYY-MM-DD, step=<paso>, company=<uuid> opcional. */
export async function GET(request: NextRequest) {
  const blocked = await opsApiGuard();
  if (blocked) return blocked;
  const query = parseItemsQuery(request.nextUrl.searchParams);
  if (!query.ok) return opsBadRequest([query.error]);
  try {
    const result = await listItems(query.value, await listOpsCompanies());
    return opsJson({
      source: query.value.source,
      day: result.day,
      company: query.value.company,
      step: { key: result.step.key, title: result.step.title, hint: result.step.hint, shared: result.step.shared },
      total: result.total,
      limit: result.limit,
      items: result.items,
    });
  } catch (error) {
    return opsServerError(error);
  }
}
