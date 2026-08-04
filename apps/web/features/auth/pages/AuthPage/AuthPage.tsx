"use client";

import { useState } from "react";
import { signIn } from "next-auth/react";
import Link from "next/link";
import { ArrowRight, Eye, EyeOff } from "lucide-react";
import { BrandLogo } from "@/components/ui/BrandLogo";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { RadarScene } from "../../components/RadarScene";
import styles from "./AuthPage.module.css";

const errorLabels: Record<string, string> = {
  CredentialsSignin: "No pudimos iniciar sesion con esos datos.",
  Configuration: "Falta configuracion de autenticacion.",
};

export function AuthPage({ error, mode = "login" }: { error?: string | null; mode?: "login" | "register" }) {
  const [formError, setFormError] = useState(error ?? null);
  const [loading, setLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setFormError(null);
    const form = new FormData(event.currentTarget);
    const result = await signIn("credentials", {
      redirect: false,
      callbackUrl: mode === "register" ? "/onboarding" : "/feed",
      email: String(form.get("email") ?? ""),
      password: String(form.get("password") ?? ""),
      name: String(form.get("name") ?? ""),
      tenant_name: String(form.get("tenant_name") ?? ""),
    });
    setLoading(false);
    if (result?.ok) {
      window.location.href = result.url ?? (mode === "register" ? "/onboarding" : "/feed");
      return;
    }
    setFormError("CredentialsSignin");
  }

  return (
    <main className={styles.page}>
      <section className={styles.canvas}>
        <div className={styles.brandPane}>
          <Link className={styles.logo} href="/" aria-label="BuenaPro, inicio">
            <BrandLogo light priority />
          </Link>
          <div className={styles.radarStage}><RadarScene /></div>
          <div className={styles.brandMessage}>
            <span>Radar de oportunidades</span>
            <h1>{mode === "register" ? "Encuentra dónde competir." : "Detecta. Evalúa. Decide."}</h1>
            <p>Señales de SEACE convertidas en decisiones claras para tu empresa.</p>
          </div>
        </div>
        <div className={styles.panel}>
          <div className={styles.formContainer}>
            <div className={styles.formHeader}>
              <span>{mode === "register" ? "Empieza tu radar" : "Acceso seguro"}</span>
              <h2>{mode === "register" ? "Crea tu cuenta" : "Ingresa a tu cuenta"}</h2>
              <p>{mode === "register" ? "Configura los datos basicos de tu empresa." : "Continua donde dejaste tus oportunidades."}</p>
            </div>
            {formError ? <p className={styles.error}>{errorLabels[formError] ?? "No se pudo iniciar sesion."}</p> : null}
            <form className={styles.form} onSubmit={submit}>
              {mode === "register" ? (
                <>
                  <label>Tu nombre<Input autoComplete="name" name="name" required /></label>
                  <label>Empresa<Input autoComplete="organization" name="tenant_name" required /></label>
                </>
              ) : null}
              <label>Correo electronico<Input autoComplete="email" name="email" required type="email" /></label>
              <label>Contraseña<span className={styles.passwordField}><Input autoComplete={mode === "register" ? "new-password" : "current-password"} minLength={8} name="password" required type={showPassword ? "text" : "password"} /><button aria-label={showPassword ? "Ocultar contraseña" : "Mostrar contraseña"} onClick={() => setShowPassword((value) => !value)} type="button">{showPassword ? <EyeOff size={18} /> : <Eye size={18} />}</button></span></label>
              <Button disabled={loading} type="submit">
                {loading ? "Procesando..." : mode === "register" ? <>Crear cuenta <ArrowRight size={17} /></> : <>Entrar <ArrowRight size={17} /></>}
              </Button>
            </form>
            <p className={styles.demo}>
              {mode === "register" ? <>¿Ya tienes una cuenta? <Link href="/login">Ingresar</Link></> : <>¿Primera vez en BuenaPro? <Link href="/registro">Crear cuenta</Link></>}
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
