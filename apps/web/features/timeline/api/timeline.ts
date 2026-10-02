import type { TimelineResponse } from "@/lib/procurementTimeline";

export async function refreshTimeline(source: string, q: string): Promise<TimelineResponse> {
  const response = await fetch(`/api/cronograma?${new URLSearchParams({ source, q })}`, { cache: "no-store" });
  if (!response.ok) throw new Error("No se pudo actualizar el cronograma. Intenta nuevamente.");
  const payload = await response.json();
  return payload;
}
