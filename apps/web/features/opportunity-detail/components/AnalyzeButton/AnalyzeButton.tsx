"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/Button";

type Status = "idle" | "queued" | "error" | "no_profile";

export function AnalyzeButton({
  idContrato,
  profileId,
  analyzedAt,
  children = "Evaluar con mi perfil",
}: {
  idContrato: string | number;
  profileId?: string | null;
  analyzedAt?: string | null;
  children?: React.ReactNode;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<Status>("idle");
  async function analyze() {
    setStatus("queued");
    try {
      const response = await fetch(`/api/contracts/${idContrato}/analyze`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ force: Boolean(analyzedAt), ...(profileId ? { profile_id: profileId } : {}) }),
      });
      if (response.status === 409) {
        setStatus("no_profile");
        return;
      }
      if (!response.ok) {
        setStatus("error");
        return;
      }
      router.refresh();
      setStatus("idle");
    } catch {
      setStatus("error");
    }
  }

  if (status === "no_profile") {
    return (
      <a href="/perfil">
        <Button variant="secondary">Configura tu perfil para evaluar</Button>
      </a>
    );
  }

  return (
    <Button
      disabled={status === "queued"}
      onClick={analyze}
      variant={analyzedAt ? "secondary" : "primary"}
    >
      {status === "queued"
        ? "Analizando con IA…"
        : status === "error"
          ? "Reintentar análisis"
          : children}
    </Button>
  );
}
