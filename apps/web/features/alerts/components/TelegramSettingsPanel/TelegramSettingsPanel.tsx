"use client";

import { useEffect, useState } from "react";
import { Bot, CircleCheck, Plus, Send, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Switch } from "@/components/ui/Switch";
import {
  getTelegramIntegration,
  testTelegramRecipient,
  updateTelegramIntegration,
} from "../../api/settings";
import type { TelegramIntegration, TelegramRecipient } from "../../model/types";
import styles from "./TelegramSettingsPanel.module.css";

type EditableRecipient = TelegramRecipient & { localKey: string };

const emptyIntegration: TelegramIntegration = {
  configured: false,
  enabled: false,
  recipients: [],
};

function editableRecipient(recipient?: Partial<TelegramRecipient>): EditableRecipient {
  return {
    id: recipient?.id,
    label: recipient?.label ?? "Mi Telegram",
    chat_id: recipient?.chat_id ?? "",
    enabled: recipient?.enabled ?? true,
    localKey: recipient?.id != null ? String(recipient.id) : crypto.randomUUID(),
  };
}

export function TelegramSettingsPanel() {
  const [integration, setIntegration] = useState<TelegramIntegration>(emptyIntegration);
  const [recipients, setRecipients] = useState<EditableRecipient[]>([]);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | number | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const data = await getTelegramIntegration();
    setIntegration(data);
    setRecipients((data.recipients ?? []).map(editableRecipient));
  }

  useEffect(() => {
    let active = true;
    getTelegramIntegration()
      .then((data) => {
        if (!active) return;
        setIntegration(data);
        setRecipients((data.recipients ?? []).map(editableRecipient));
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : "No se pudo cargar Telegram");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  function updateRecipient(localKey: string, patch: Partial<EditableRecipient>) {
    setRecipients((current) => current.map((recipient) => recipient.localKey === localKey ? { ...recipient, ...patch } : recipient));
    setMessage("");
  }

  async function save(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await updateTelegramIntegration({
        ...(token.trim() ? { token: token.trim() } : {}),
        enabled: integration.enabled,
        recipients: recipients.map(({ localKey: _localKey, ...recipient }) => recipient),
      });
      setToken("");
      await load();
      setMessage("Conexión de Telegram guardada");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo guardar Telegram");
    } finally {
      setSaving(false);
    }
  }

  async function test(recipient: EditableRecipient) {
    if (!recipient.id) {
      setError("Guarda este destinatario antes de enviar una prueba");
      return;
    }
    setTestingId(recipient.id);
    setError("");
    setMessage("");
    try {
      await testTelegramRecipient(recipient.id);
      setMessage(`Mensaje de prueba enviado a ${recipient.label}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No se pudo enviar la prueba");
    } finally {
      setTestingId(null);
    }
  }

  const botName = integration.bot_username ? `@${integration.bot_username.replace(/^@/, "")}` : "Bot sin verificar";

  return (
    <form className={styles.panel} onSubmit={save} aria-labelledby="telegram-title">
      <header className={styles.header}>
        <span className={styles.icon} aria-hidden="true"><Bot size={20} /></span>
        <div>
          <h2 id="telegram-title">Telegram</h2>
          <p>Envía oportunidades evaluadas a uno o varios chats.</p>
        </div>
        <Switch
          checked={integration.enabled}
          disabled={loading}
          label={integration.enabled ? "Activo" : "Pausado"}
          onChange={(event) => setIntegration((current) => ({ ...current, enabled: event.target.checked }))}
        />
      </header>

      <div className={styles.body} aria-busy={loading}>
        <div className={styles.connection}>
          <span className={integration.configured ? styles.connectedIcon : styles.pendingIcon} aria-hidden="true">
            {integration.configured ? <CircleCheck size={19} /> : <Bot size={19} />}
          </span>
          <div>
            <strong>{integration.configured ? botName : "Conecta tu bot"}</strong>
            <span>
              {integration.configured
                ? `Token protegido${integration.token_hint ? ` · termina en ${integration.token_hint}` : ""}`
                : "El token se guarda de forma segura y nunca vuelve a mostrarse."}
            </span>
          </div>
          <span className={integration.configured ? styles.connected : styles.pending}>
            {integration.configured ? "Configurado" : "Pendiente"}
          </span>
        </div>

        <label className={styles.tokenField}>
          <span>{integration.configured ? "Reemplazar token del bot" : "Token del bot"}</span>
          <Input
            aria-describedby="telegram-token-help"
            autoComplete="off"
            disabled={loading}
            name="telegram_token"
            onChange={(event) => setToken(event.target.value)}
            placeholder={integration.configured ? "Déjalo vacío para conservar el token actual…" : "Pega el token entregado por BotFather…"}
            required={!integration.configured}
            spellCheck={false}
            type="password"
            value={token}
          />
          <small id="telegram-token-help">El token solo se envía al guardar y no se incluye en la interfaz.</small>
        </label>

        <section className={styles.recipients} aria-labelledby="telegram-recipients-title">
          <div className={styles.recipientsHeader}>
            <div>
              <h3 id="telegram-recipients-title">Destinatarios</h3>
              <p>Cada chat activo recibirá las mismas oportunidades aprobadas.</p>
            </div>
            <Button
              disabled={loading}
              onClick={() => setRecipients((current) => [...current, editableRecipient()])}
              size="compact"
              type="button"
              variant="secondary"
            >
              <Plus size={16} /> Agregar chat
            </Button>
          </div>

          {recipients.length ? (
            <div className={styles.recipientList}>
              {recipients.map((recipient, index) => (
                <div className={styles.recipient} key={recipient.localKey}>
                  <label>
                    <span>Nombre</span>
                    <Input
                      aria-label={`Nombre del destinatario ${index + 1}`}
                      disabled={loading}
                      maxLength={80}
                      name={`recipient_label_${index}`}
                      onChange={(event) => updateRecipient(recipient.localKey, { label: event.target.value })}
                      placeholder="Ej. Telegram personal…"
                      autoComplete="off"
                      required
                      value={recipient.label}
                    />
                  </label>
                  <label>
                    <span>Chat ID</span>
                    <Input
                      aria-label={`Chat ID del destinatario ${index + 1}`}
                      disabled={loading}
                      inputMode="numeric"
                      name={`recipient_chat_id_${index}`}
                      onChange={(event) => updateRecipient(recipient.localKey, { chat_id: event.target.value })}
                      placeholder="Ej. 123456789…"
                      autoComplete="off"
                      spellCheck={false}
                      required
                      value={recipient.chat_id}
                    />
                  </label>
                  <Switch
                    checked={recipient.enabled}
                    disabled={loading}
                    label={recipient.enabled ? "Activo" : "Pausado"}
                    onChange={(event) => updateRecipient(recipient.localKey, { enabled: event.target.checked })}
                  />
                  <div className={styles.recipientActions}>
                    <button
                      aria-label={`Enviar prueba a ${recipient.label || `destinatario ${index + 1}`}`}
                      className={styles.iconButton}
                      disabled={!recipient.id || testingId === recipient.id}
                      onClick={() => test(recipient)}
                      title="Enviar mensaje de prueba"
                      type="button"
                    >
                      <Send size={17} />
                    </button>
                    <button
                      aria-label={`Eliminar ${recipient.label || `destinatario ${index + 1}`}`}
                      className={styles.iconButton}
                      onClick={() => setRecipients((current) => current.filter((item) => item.localKey !== recipient.localKey))}
                      title="Eliminar destinatario"
                      type="button"
                    >
                      <Trash2 size={17} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className={styles.empty}>
              <Send size={20} />
              <div><strong>No hay chats agregados</strong><span>Agrega al menos uno para recibir alertas.</span></div>
            </div>
          )}
        </section>
      </div>

      <footer className={styles.footer}>
        <span className={error ? styles.error : styles.status} role={error ? "alert" : "status"} aria-live="polite">
          {error || message}
        </span>
        <Button disabled={loading || saving} type="submit">{saving ? "Guardando…" : "Guardar Telegram"}</Button>
      </footer>
    </form>
  );
}
