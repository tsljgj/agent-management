// One subscription turn. Never forward raw CLI errors or credential data.
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";

// SDK 0.162.0 does not set windowsHide on its native CLI child.
if (process.platform === "win32") {
  const spawn = childProcess.spawn;
  childProcess.spawn = (...args) => {
    args[2] = { ...(args[2] ?? {}), windowsHide: true };
    return spawn(...args);
  };
  syncBuiltinESMExports();
}

const [entry, cwd, timeout] = process.argv.slice(2);
const allowedEnvironment = new Set([
  "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
  "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "HOME", "USERPROFILE",
  "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
  "TMP", "TEMP", "TMPDIR", "CODEX_HOME",
]);
const cliEnvironment = Object.fromEntries(Object.entries(process.env).filter(
  ([key]) => allowedEnvironment.has(key.toUpperCase()),
));
const abort = new AbortController();
const timer = setTimeout(() => abort.abort(), Number(timeout) * 1000);
const forbidden = new Set(["command_execution", "file_change", "mcp_tool_call", "web_search"]);

try {
  const { Codex } = await import(pathToFileURL(entry).href);
  const codex = new Codex({
    env: cliEnvironment,
    config: {
      model_provider: "openai",
      forced_login_method: "chatgpt",
      cli_auth_credentials_store: "file",
      project_doc_max_bytes: 0,
      history: { persistence: "none" },
      features: {
        shell_tool: false,
        unified_exec: false,
        shell_snapshot: false,
        apps: false,
        hooks: false,
        multi_agent: false,
        memories: false,
        goals: false,
        plugins: false,
      },
      developer_instructions: "Only reply OK. Do not use tools, read files, inspect directories, or perform any other task.",
    },
  });
  const thread = codex.startThread({
    workingDirectory: cwd,
    skipGitRepoCheck: true,
    sandboxMode: "read-only",
    approvalPolicy: "never",
    networkAccessEnabled: false,
    webSearchMode: "disabled",
    modelReasoningEffort: "low",
  });
  const { events } = await thread.runStreamed("Reply with exactly OK.", { signal: abort.signal });
  let completed = false;
  for await (const event of events) {
    if (event.item && forbidden.has(event.item.type)) {
      abort.abort();
      throw Object.assign(new Error(), { safeKind: "unexpected-tools" });
    }
    if (event.type === "turn.failed" || event.type === "error") throw new Error(event.error?.message ?? event.message);
    if (event.type === "turn.completed") completed = true;
  }
  if (!completed) throw new Error();
  process.stdout.write(JSON.stringify({ ok: true }));
} catch (error) {
  const message = String(error?.message ?? "").toLowerCase();
  const kind = error?.safeKind ?? (abort.signal.aborted ? "timeout" :
    /unauthori|authentication|token|log.?in|401/.test(message) ? "auth" : "sdk");
  process.stdout.write(JSON.stringify({ ok: false, error: kind }));
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
}
