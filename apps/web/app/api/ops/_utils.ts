import { NextResponse } from "next/server";

const NO_STORE = { "Cache-Control": "no-store" };

export function opsJson(data: unknown, status = 200) {
  return NextResponse.json(data, { status, headers: NO_STORE });
}

export function opsBadRequest(parameters: string[]) {
  return opsJson({ error: "Invalid parameters", parameters }, 400);
}

/** Error interno sin detalles: el mensaje real queda solo en el log del servidor. */
export function opsServerError(error: unknown) {
  console.error("ops api error", error instanceof Error ? error.message : error);
  return opsJson({ error: "Internal server error" }, 500);
}
