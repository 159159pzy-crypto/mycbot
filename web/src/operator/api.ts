export class OperatorAuthError extends Error {}

export type ConversationSummary = {
  id: string;
  stable_key: string;
  platform: string;
  chat_kind: string;
  chat_id: string;
  last_message_at: string | null;
  message_count: number;
};

export type MessageView = {
  direction: string;
  sender_identity_id: string;
  text: string;
  occurred_at: string | null;
};

export type ToolInvocationView = {
  tool_id: string;
  ok: boolean;
  error_code: string | null;
  latency_ms: number;
};

export type TurnView = {
  id: string;
  action: string;
  trigger: string;
  model: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  outcome: string;
  error: string | null;
  created_at: string | null;
  tool_invocations: ToolInvocationView[];
};

export type MemoryView = {
  id: string;
  scope: string;
  subject_identity_id: string | null;
  conversation_stable_key: string | null;
  kind: string;
  content: string;
  confidence: number;
  privacy: string;
  revoked_at: string | null;
  revoked_reason: string | null;
  created_at: string | null;
};

export type UsagePoint = { day: string; turns: number; tokens: number };

export type MetricsView = {
  turns: {
    by_outcome: Record<string, number>;
    avg_latency_ms: number;
    p95_latency_ms: number;
  };
  queues: Record<string, number>;
};

export type PluginView = {
  id: string;
  version: string;
  runner_id: string;
  tools: string[];
  event_hooks: string[];
  granted_capabilities: string[];
};

export type PersonaConfig = { override: string | null; default: string };

export type OperatorClient = {
  get: <T>(path: string) => Promise<T>;
  send: <T>(method: 'POST' | 'PUT', path: string, body?: unknown) => Promise<T>;
};

export function createOperatorClient(
  token: string,
  onAuthError: () => void,
): OperatorClient {
  const request = async <T>(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<T> => {
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 401) {
      onAuthError();
      throw new OperatorAuthError('the operator token was rejected');
    }
    if (!response.ok) {
      throw new Error(`operator API returned HTTP ${response.status}`);
    }
    return (await response.json()) as T;
  };
  return {
    get: <T>(path: string) => request<T>('GET', path),
    send: <T>(method: 'POST' | 'PUT', path: string, body?: unknown) =>
      request<T>(method, path, body),
  };
}
