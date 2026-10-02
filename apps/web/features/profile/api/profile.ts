import { apiFetch } from "@/lib/api/client";

export function saveProfile(payload: Record<string, unknown>) {
  return apiFetch<{ data: any }>("/api/profile", { method: "PUT", json: payload });
}

export async function uploadProfileEvidence(file: File, title: string) {
  const form = new FormData();
  form.set("file", file);
  form.set("title", title || file.name);
  form.set("tags", "respaldo del perfil");
  form.set("usableForApplications", "on");
  const response = await fetch("/api/profile/library/documents", { method: "POST", body: form });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error ?? "No se pudo subir el archivo. Intenta nuevamente.");
  window.dispatchEvent(new Event("profile-library-changed"));
  return payload.data as { id: string; title: string; downloadUrl: string; filename: string };
}
