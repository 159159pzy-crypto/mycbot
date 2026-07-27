export class OperatorAuthError extends Error {}

export type ConversationSummary = {
  id: string;
  stable_key: string;
  platform: string;
  chat_kind: string;
  chat_id: string;
  last_message_at: string | null;
  message_count: number;
  ephemeral?: boolean;
};

export type MessageView = {
  direction: string;
  sender_identity_id: string;
  text: string;
  occurred_at: string | null;
  trace_id?: string | null;
  segments?: Array<Record<string, unknown>>;
};

export type TraceSpanView = {
  id: string;
  trace_id: string;
  message_id: string | null;
  stage: string;
  status: 'ok' | 'error' | 'skipped';
  duration_ms: number;
  attributes: Record<string, unknown>;
  created_at: string | null;
};

export type SandboxSessionView = {
  status: 'pending' | 'ready';
  session_id: string;
  conversation?: ConversationSummary;
  messages?: MessageView[];
  turns?: TurnView[];
  traces?: TraceSpanView[];
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
  invalid_at: string | null;
  invalidated_by: string | null;
  supersedes: string[];
  state: 'active' | 'invalidated' | 'revoked';
  created_at: string | null;
};

export type MemoryOperationView = {
  id: string;
  operation: string;
  source: string;
  memory_id: string | null;
  previous_memory_id: string | null;
  detail: Record<string, unknown>;
  created_at: string | null;
};

export type CoreBlockView = {
  id: string;
  label: 'persona' | 'user_profile';
  subject_identity_id: string | null;
  content: string;
  token_budget: number;
  version: number;
  created_at: string;
  updated_at: string;
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

export type ModelTarget = {
  model: string;
  input_price_per_million?: string | null;
  output_price_per_million?: string | null;
};

export type ModelChannel = {
  name: string;
  base_url: string;
  api_key_env: string | null;
  priority: number;
  weight: number;
  enabled: boolean;
  model_map: Record<string, ModelTarget>;
};

export type ModelChannelUsage = {
  channel: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd_micros: number;
  last_status: string;
  last_called_at: string | null;
};

export type ModelDailyUsage = {
  day: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd_micros: number;
};

export type ModelConversationUsage = {
  conversation_id: string | null;
  stable_key: string | null;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd_micros: number;
};

export type ModelsView = {
  source: 'runtime' | 'legacy';
  channels: ModelChannel[];
  usage: ModelChannelUsage[];
  daily_usage: ModelDailyUsage[];
  conversation_usage: ModelConversationUsage[];
};

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
