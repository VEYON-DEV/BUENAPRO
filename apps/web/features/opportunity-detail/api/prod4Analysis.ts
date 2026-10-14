export type AnalysisJob = { job_id: string; status: string; error?: string };
export async function prod4AnalysisRequest(id: string, options: { force?: boolean; jobId?: string; signal?: AbortSignal }): Promise<AnalysisJob> {
  const url = `/api/prod4/opportunities/${encodeURIComponent(id)}/analyze`;
  const response = await fetch(options.jobId ? `${url}?job_id=${encodeURIComponent(options.jobId)}` : url, options.jobId
    ? { signal: options.signal, cache: "no-store" }
    : { method: "POST", signal: options.signal, headers: { "content-type": "application/json" }, body: JSON.stringify({ force: options.force }) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "No se pudo analizar. Intenta nuevamente.");
  return result.data;
}
