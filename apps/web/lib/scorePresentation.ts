/** Display tone only: it never changes the stored verdict or validates a score. */
export function scoreTone(score: unknown): "rojo" | "ambar" | "verde" | "gris" {
  if (typeof score !== "number" || !Number.isFinite(score) || score < 0 || score > 100) return "gris";
  if (score < 30) return "rojo";
  if (score <= 80) return "ambar";
  return "verde";
}
