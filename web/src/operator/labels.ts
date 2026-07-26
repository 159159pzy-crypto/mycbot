export type Tone = 'green' | 'gray' | 'orange' | 'red' | 'blue';

export const OUTCOME_META: Record<string, { label: string; tone: Tone }> = {
  replied: { label: '已回复', tone: 'green' },
  fallback: { label: '已回退', tone: 'gray' },
  budget_exceeded: { label: '超出预算', tone: 'orange' },
  error: { label: '失败', tone: 'red' },
};

export function outcomeMeta(outcome: string): { label: string; tone: Tone } {
  return OUTCOME_META[outcome] ?? { label: outcome, tone: 'gray' };
}

export const TRIGGER_LABEL: Record<string, string> = {
  DIRECT_MESSAGE: '私聊触发',
  MENTION: '提及触发',
  REPLY: '回复触发',
  COMMAND: '命令触发',
  PROACTIVE: '主动触发',
  POLICY: '策略触发',
};

export function triggerLabel(trigger: string): string {
  return TRIGGER_LABEL[trigger] ?? trigger;
}

export const PLATFORM_LABEL: Record<string, string> = {
  QQ: 'QQ',
  TELEGRAM: 'Telegram',
};

export function platformLabel(platform: string): string {
  return PLATFORM_LABEL[platform] ?? platform;
}

export const CHAT_KIND_LABEL: Record<string, string> = {
  DIRECT: '私聊',
  GROUP: '群聊',
  CHANNEL: '频道',
};

export function chatKindLabel(kind: string): string {
  return CHAT_KIND_LABEL[kind] ?? kind;
}

export const SCOPE_LABEL: Record<string, string> = {
  SUBJECT: '主体',
  CONVERSATION: '会话',
  GLOBAL: '全局',
};

export function scopeLabel(scope: string): string {
  return SCOPE_LABEL[scope] ?? scope;
}

export const PRIVACY_LABEL: Record<string, string> = {
  PRIVATE: '私人',
  SHARED: '共享',
  PUBLIC: '公开',
  SENSITIVE: '敏感',
};

export function privacyLabel(privacy: string): string {
  return PRIVACY_LABEL[privacy] ?? privacy;
}

export const MEMORY_KIND_LABEL: Record<string, string> = {
  preference: '偏好',
  fact: '事实',
  context: '上下文',
};

export function memoryKindLabel(kind: string): string {
  return MEMORY_KIND_LABEL[kind] ?? kind;
}
