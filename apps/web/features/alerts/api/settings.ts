import { apiFetch } from "@/lib/api/client";
import type { AutomationRules, TelegramIntegration, TelegramRecipient } from "../model/types";

type DataResponse<T> = { data: T };

export async function getAutomationRules() {
  return (await apiFetch<DataResponse<AutomationRules>>("/api/automation/rules")).data;
}

export async function updateAutomationRules(rules: AutomationRules) {
  return (
    await apiFetch<DataResponse<AutomationRules>>("/api/automation/rules", {
      method: "PUT",
      json: rules,
    })
  ).data;
}

export async function getTelegramIntegration() {
  return (await apiFetch<DataResponse<TelegramIntegration>>("/api/integrations/telegram")).data;
}

export async function updateTelegramIntegration(input: {
  token?: string;
  enabled: boolean;
  recipients: TelegramRecipient[];
}) {
  return (
    await apiFetch<DataResponse<TelegramIntegration>>("/api/integrations/telegram", {
      method: "PUT",
      json: input,
    })
  ).data;
}

export async function testTelegramRecipient(recipientId: string | number) {
  return apiFetch<DataResponse<{ sent: boolean }>>("/api/integrations/telegram/test", {
    method: "POST",
    json: { recipient_id: recipientId },
  });
}
