type AnalysisInput = {
  matchId?: string | number | null;
  score?: string | number | null;
  verdict?: string | null;
  reviewRequired?: boolean;
};

/** A withheld score is still an existing analysis; a gray verdict alone is not. */
export function analysisPresentation({ matchId, score, verdict, reviewRequired }: AnalysisInput) {
  const hasMatch = typeof matchId === "number"
    ? Number.isFinite(matchId) && matchId > 0
    : typeof matchId === "string" && matchId.trim() !== "" && matchId.trim() !== "0";
  const hasScore = (typeof score === "number" || typeof score === "string" && score.trim() !== "")
    && Number.isFinite(Number(score)) && Number(score) >= 0 && Number(score) <= 100 && Boolean(verdict);
  if (hasMatch && (reviewRequired || !hasScore)) return "review";
  // Numeric zero and legacy scores remain published when there is no review flag.
  if (hasScore) return "evaluated";
  return "none";
}

/** Only the explicit public provisional field can supply a retained score. */
export function provisionalAnalysisScore(input: AnalysisInput, provisionalScore: unknown): number | null {
  if (analysisPresentation(input) !== "review") return null;
  return typeof provisionalScore === "number" && Number.isFinite(provisionalScore)
    && provisionalScore >= 0 && provisionalScore <= 100 ? provisionalScore : null;
}

export function analysisFilterHref(defaults: Record<string, string>, source?: "prod2" | "prod4") {
  const next = new URLSearchParams(defaults);
  if (source) next.set("source", source);
  next.delete("page");
  if (defaults.analysis === "done") next.delete("analysis");
  else next.set("analysis", "done");
  return `/feed?${next.toString()}`;
}

/** Numeric-score filtering is separate from the legacy saved-analysis filter. */
export function scoreFilterHref(defaults: Record<string, string>, source?: "prod2" | "prod4") {
  const next = new URLSearchParams(defaults);
  if (source) next.set("source", source);
  next.delete("page");
  next.delete("analysis");
  if (defaults.with_score === "true") next.delete("with_score");
  else next.set("with_score", "true");
  return `/feed?${next.toString()}`;
}
