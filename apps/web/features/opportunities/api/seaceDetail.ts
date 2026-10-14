export async function seaceDetailRequest<T>(source: "prod2" | "prod4", id: string, profileId?: string) {
  const params = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
  const response = await fetch(`/api/${source}/opportunities/${encodeURIComponent(id)}${params}`, { cache: "no-store" });
  if (!response.ok) throw new Error("No se pudo cargar la ficha");
  return response.json() as Promise<T>;
}
