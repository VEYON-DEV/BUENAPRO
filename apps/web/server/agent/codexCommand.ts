export type CodexSandboxMode =
  | "read-only"
  | "workspace-write"
  | "danger-full-access";

export function codexSandboxMode(
  value = process.env.CODEX_AGENT_SANDBOX_MODE,
): CodexSandboxMode {
  const configured = value ?? "workspace-write";
  if (
    configured === "read-only" ||
    configured === "workspace-write" ||
    configured === "danger-full-access"
  ) {
    return configured;
  }
  return "workspace-write";
}

export function buildCodexArgs(options: {
  prompt: string;
  schemaPath: string;
  workspacePath: string;
  threadId?: string | null;
  model?: string;
  sandboxMode?: CodexSandboxMode;
}) {
  const model = options.model ?? process.env.CODEX_AGENT_MODEL;
  const sandboxMode = options.sandboxMode ?? codexSandboxMode();
  const globalArgs = ["exec", "--sandbox", sandboxMode];

  if (options.threadId) {
    return [
      ...globalArgs,
      "resume",
      "--json",
      "--skip-git-repo-check",
      "--output-schema",
      options.schemaPath,
      ...(model ? ["-m", model] : []),
      options.threadId,
      options.prompt,
    ];
  }

  return [
    ...globalArgs,
    "--json",
    "--skip-git-repo-check",
    "--output-schema",
    options.schemaPath,
    ...(model ? ["-m", model] : []),
    "-C",
    options.workspacePath,
    options.prompt,
  ];
}
