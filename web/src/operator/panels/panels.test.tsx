import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorClient } from '../api';
import { ConversationsPanel } from './ConversationsPanel';
import { MemoriesPanel } from './MemoriesPanel';
import { ModelsPanel } from './ModelsPanel';
import { OverviewPanel } from './OverviewPanel';
import { PersonaPanel } from './PersonaPanel';
import { PluginsPanel } from './PluginsPanel';
import { SandboxPanel } from './SandboxPanel';

function stubClient(routes: Record<string, unknown>): OperatorClient & {
  sent: Array<{ method: string; path: string; body: unknown }>;
} {
  const sent: Array<{ method: string; path: string; body: unknown }> = [];
  const resolve = (path: string): unknown => {
    const match = Object.entries(routes).find(([prefix]) => path.startsWith(prefix));
    if (!match) throw new Error(`no stub for ${path}`);
    return typeof match[1] === 'function' ? (match[1] as (p: string) => unknown)(path) : match[1];
  };
  return {
    sent,
    get: <T,>(path: string) => Promise.resolve(resolve(path) as T),
    send: <T,>(method: 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown) => {
      sent.push({ method, path, body });
      return Promise.resolve(resolve(path) as T);
    },
    upload: <T,>(path: string, body: FormData) => {
      sent.push({ method: 'POST', path, body });
      return Promise.resolve(resolve(path) as T);
    },
  };
}

describe('OverviewPanel', () => {
  it('renders usage totals, chart, queues, and outcomes', async () => {
    const client = stubClient({
      '/operator/usage': {
        usage: [
          { day: '2026-07-25', turns: 2, tokens: 100 },
          { day: '2026-07-26', turns: 3, tokens: 340 },
        ],
      },
      '/operator/metrics': {
        turns: { by_outcome: { replied: 4, error: 1 }, avg_latency_ms: 750, p95_latency_ms: 1900 },
        queues: { ingest: 2, outbound_dead_letter: 1 },
      },
    });

    render(<OverviewPanel client={client} />);

    await waitFor(() => expect(screen.getByText('440')).toBeInTheDocument());
    expect(
      screen.getByText('延迟 平均 / P95').closest('.stat-tile'),
    ).toHaveTextContent('750 ms / 1900 ms');
    expect(
      screen.getByRole('img', { name: /Token 用量趋势，07-25 至 07-26/ }),
    ).toBeInTheDocument();
    expect(screen.getByText('outbound_dead_letter')).toBeInTheDocument();
    expect(screen.getByText('已回复')).toBeInTheDocument();
    expect(screen.getByText('失败')).toBeInTheDocument();
  });
});

describe('ConversationsPanel', () => {
  it('lists conversations and opens turn and tool detail', async () => {
    const client = stubClient({
      '/operator/conversations/conv-1/messages': {
        messages: [
          {
            direction: 'inbound',
            sender_identity_id: 'telegram:777',
            text: '讲个笑话',
            occurred_at: null,
          },
          { direction: 'outbound', sender_identity_id: 'self', text: '好的', occurred_at: null },
        ],
      },
      '/operator/conversations/conv-1/turns': {
        turns: [
          {
            id: 'turn-1',
            action: 'AGENT',
            trigger: 'DIRECT_MESSAGE',
            model: 'deepseek-chat',
            prompt_tokens: 100,
            completion_tokens: 40,
            latency_ms: 900,
            outcome: 'replied',
            error: null,
            created_at: null,
            tool_invocations: [
              { tool_id: 'web_search', ok: false, error_code: 'timeout', latency_ms: 15000 },
            ],
          },
        ],
      },
      '/operator/conversations/conv-1/traces': {
        traces: [
          {
            id: 'span-1',
            trace_id: 'trace-1',
            message_id: null,
            stage: 'memory.retrieval',
            status: 'ok',
            duration_ms: 12,
            attributes: { memory: '喜欢美式咖啡' },
            created_at: null,
          },
        ],
      },
      '/operator/conversations': {
        conversations: [
          {
            id: 'conv-1',
            stable_key: 'v1:telegram-main:DIRECT:777:0',
            platform: 'TELEGRAM',
            chat_kind: 'DIRECT',
            chat_id: '777',
            last_message_at: null,
            message_count: 2,
          },
        ],
      },
    });

    render(<ConversationsPanel client={client} />);

    const row = await screen.findByRole('button', { name: /Telegram.*777/ });
    expect(row).toHaveTextContent('私聊');
    fireEvent.click(row);

    await waitFor(() => expect(screen.getByText('讲个笑话')).toBeInTheDocument());
    expect(screen.getByText('已回复')).toBeInTheDocument();
    expect(screen.getByText(/私聊触发/)).toBeInTheDocument();
    expect(screen.getByText(/工具: web_search\(timeout\)/)).toBeInTheDocument();
    expect(screen.getByText(/140 tok · 900 ms/)).toBeInTheDocument();
    expect(screen.getByText('memory.retrieval')).toBeInTheDocument();
    fireEvent.click(screen.getByText('查看阶段属性'));
    expect(screen.getByText(/喜欢美式咖啡/)).toBeInTheDocument();
  });
});

describe('SandboxPanel', () => {
  it('submits a multimodal message and renders reply and trace data', async () => {
    const client = stubClient({
      '/operator/sandbox/messages': {
        accepted: true,
        session_id: 'debug-1',
        trace_id: 'trace-1',
        envelope_id: 'sandbox:1',
      },
      '/operator/sandbox/': {
        status: 'ready',
        session_id: 'debug-1',
        messages: [
          {
            direction: 'inbound',
            sender_identity_id: 'sandbox:operator',
            text: '看图',
            occurred_at: null,
            trace_id: 'trace-1',
          },
          {
            direction: 'outbound',
            sender_identity_id: 'self',
            text: '是一只猫',
            occurred_at: null,
            trace_id: 'trace-1',
          },
        ],
        turns: [],
        traces: [
          {
            id: 'span-vision',
            trace_id: 'trace-1',
            message_id: null,
            stage: 'vision.resolve',
            status: 'ok',
            duration_ms: 24,
            attributes: {},
            created_at: null,
          },
        ],
      },
    });

    render(<SandboxPanel client={client} />);
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '看图' } });
    fireEvent.change(screen.getByLabelText('图片 URL（可选）'), {
      target: { value: 'https://img.example/cat.png' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送到真实链路' }));

    await screen.findByText('是一只猫');
    expect(screen.getByText('vision.resolve')).toBeInTheDocument();
    expect(client.sent[0]).toMatchObject({
      method: 'POST',
      path: '/operator/sandbox/messages',
      body: {
        text: '看图',
        image_urls: ['https://img.example/cat.png'],
      },
    });
  });
});

describe('MemoriesPanel', () => {
  it('renders memories and revokes through the API', async () => {
    let revoked = false;
    const client = stubClient({
      '/operator/memories/mem-1/revoke': () => {
        revoked = true;
        return { revoked: true };
      },
      '/operator/memories': () => ({
        memories: revoked
          ? []
          : [
              {
                id: 'mem-1',
                scope: 'SUBJECT',
                subject_identity_id: 'telegram:777',
                conversation_stable_key: null,
                kind: 'preference',
                content: '喜欢美式咖啡',
                confidence: 0.9,
                privacy: 'PRIVATE',
                revoked_at: null,
                revoked_reason: null,
                created_at: null,
              },
            ],
      }),
    });

    render(<MemoriesPanel client={client} />);

    await screen.findByText('喜欢美式咖啡');
    const memList = within(screen.getByRole('list', { name: '记忆列表' }));
    expect(memList.getByText('主体')).toBeInTheDocument();
    expect(memList.getByText('私人')).toBeInTheDocument();
    expect(memList.getByText('偏好')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '撤销' }));

    await waitFor(() =>
      expect(screen.getByText('没有符合当前筛选的记忆。')).toBeInTheDocument(),
    );
    expect(client.sent[0]).toMatchObject({
      method: 'POST',
      path: '/operator/memories/mem-1/revoke',
    });
  });

  it('shows version history and saves bounded core memory', async () => {
    const baseMemory = {
      id: 'mem-2',
      scope: 'SUBJECT',
      subject_identity_id: 'telegram:777',
      conversation_stable_key: null,
      kind: 'preference',
      content: '偏好无糖美式',
      confidence: 0.9,
      privacy: 'PRIVATE',
      revoked_at: null,
      revoked_reason: null,
      invalid_at: null,
      invalidated_by: null,
      supersedes: ['mem-1'],
      state: 'active',
      created_at: null,
    };
    const client = stubClient({
      '/operator/memories/mem-2/history': {
        history: [
          { ...baseMemory, id: 'mem-1', content: '偏好咖啡', state: 'invalidated' },
          baseMemory,
        ],
      },
      '/operator/memories/mem-2/operations': {
        operations: [
          {
            id: 'op-1',
            operation: 'UPDATE',
            source: 'post_turn',
            memory_id: 'mem-2',
            previous_memory_id: 'mem-1',
            detail: {},
            created_at: null,
          },
        ],
      },
      '/operator/core-blocks/persona': { core_block: {} },
      '/operator/core-blocks': {
        core_blocks: [
          {
            id: 'core-1',
            label: 'persona',
            subject_identity_id: null,
            content: '简洁、友好',
            token_budget: 800,
            version: 2,
            created_at: '2026-07-28T00:00:00Z',
            updated_at: '2026-07-28T00:00:00Z',
          },
        ],
      },
      '/operator/memories': { memories: [baseMemory] },
    });

    render(<MemoriesPanel client={client} />);

    await screen.findByText('偏好无糖美式');
    fireEvent.click(screen.getByRole('button', { name: '版本' }));
    await screen.findByText(/invalidated · 偏好咖啡/);
    expect(screen.getByText('UPDATE · post_turn')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: /人设 · v2/ }));
    fireEvent.change(screen.getByLabelText('核心块内容'), {
      target: { value: '简洁、友好、主动核对事实' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存核心块' }));

    await waitFor(() =>
      expect(client.sent).toContainEqual({
        method: 'PUT',
        path: '/operator/core-blocks/persona',
        body: {
          subject_identity_id: null,
          content: '简洁、友好、主动核对事实',
          token_budget: 800,
        },
      }),
    );
  });
});

describe('PluginsPanel', () => {
  it('shows registered plugins and saves approvals', async () => {
    const client = stubClient({
      '/operator/plugins': {
        runners: 1,
        plugins: [
          {
            id: 'example.dice',
            version: '1.0.0',
            runner_id: 'runner-1',
            tools: ['roll_dice'],
            event_hooks: ['message'],
            granted_capabilities: [],
          },
        ],
      },
      '/operator/config/approvals': { approved_ids: ['danger_tool'] },
    });

    render(<PluginsPanel client={client} />);

    await screen.findByText('example.dice');
    expect(screen.getByText('v1.0.0')).toBeInTheDocument();
    expect(screen.getByText(/roll_dice/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('批准工具 ID'), {
      target: { value: 'new_tool' },
    });
    fireEvent.click(screen.getByRole('button', { name: '批准' }));

    await waitFor(() =>
      expect(client.sent[0]).toMatchObject({
        method: 'PUT',
        path: '/operator/config/approvals',
        body: { approved_ids: ['danger_tool', 'new_tool'] },
      }),
    );
  });
});

describe('PersonaPanel', () => {
  it('loads a profile and saves policy plus a new immutable persona version', async () => {
    const profile = {
      profile: {
        id: 'profile-1',
        name: '默认',
        description: '默认档位',
        active_persona_version_id: 'persona-1',
        model_tier: 'default',
        tool_capabilities: ['web.search'],
        memory: {
          enabled: true,
          retrieval_limit: 5,
          expression_examples: 3,
          relationship_enabled: true,
        },
        willingness: {
          enabled: false,
          threshold: 0.78,
          sensitivity: 1,
          keywords: [],
        },
      },
      persona: {
        id: 'persona-1',
        profile_id: 'profile-1',
        version: 1,
        system_prompt: 'You are MyBot.',
        parent_version_id: null,
        change_note: 'initial',
      },
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    };
    const client = stubClient({
      '/operator/profiles/profile-1/personas': { versions: [profile.persona] },
      '/operator/profiles/profile-1': { profile },
      '/operator/profiles': { profiles: [profile], bindings: [] },
      '/operator/conversations': { conversations: [] },
      '/operator/relationships': { relationships: [] },
    });
    const sendSpy = vi.spyOn(client, 'send');

    render(<PersonaPanel client={client} />);

    const textarea = await screen.findByLabelText('当前人设');
    expect(textarea).toHaveValue('You are MyBot.');

    fireEvent.change(textarea, { target: { value: '你是高冷的猫娘助手。' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() =>
      expect(screen.getByText('已保存，后续回合自动使用新配置。')).toBeInTheDocument(),
    );
    expect(sendSpy).toHaveBeenCalledWith(
      'POST',
      '/operator/profiles/profile-1/personas',
      expect.objectContaining({ system_prompt: '你是高冷的猫娘助手。' }),
    );
  });
  it('selects a remaining profile after deleting the active profile', async () => {
    const makeProfile = (id: string, name: string, prompt: string) => ({
      profile: {
        id,
        name,
        description: '',
        active_persona_version_id: `persona-${id}`,
        model_tier: 'default',
        tool_capabilities: [],
        memory: {
          enabled: true,
          retrieval_limit: 5,
          expression_examples: 3,
          relationship_enabled: true,
        },
        willingness: {
          enabled: false,
          threshold: 0.78,
          sensitivity: 1,
          keywords: [],
        },
      },
      persona: {
        id: `persona-${id}`,
        profile_id: id,
        version: 1,
        system_prompt: prompt,
        parent_version_id: null,
        change_note: 'initial',
      },
      created_at: '2026-07-28T00:00:00Z',
      updated_at: '2026-07-28T00:00:00Z',
    });
    const defaultProfile = makeProfile('profile-1', 'Default', 'Default prompt');
    const groupProfile = makeProfile('profile-2', 'Group', 'Group prompt');
    let deleted = false;
    const client = stubClient({
      '/operator/profiles/profile-1/personas': { versions: [defaultProfile.persona] },
      '/operator/profiles/profile-2/personas': { versions: [groupProfile.persona] },
      '/operator/profiles/profile-2': () => {
        deleted = true;
        return { deleted: true };
      },
      '/operator/profiles': () => ({
        profiles: deleted ? [defaultProfile] : [defaultProfile, groupProfile],
        bindings: [],
      }),
      '/operator/conversations': { conversations: [] },
      '/operator/relationships': { relationships: [] },
    });

    render(<PersonaPanel client={client} />);

    fireEvent.click(await screen.findByText('Group'));
    await waitFor(() =>
      expect(document.querySelector('.persona-prompt-label textarea')).toHaveValue('Group prompt'),
    );
    const deleteButton = document.querySelector<HTMLButtonElement>('.pill-button--danger');
    expect(deleteButton).not.toBeNull();
    fireEvent.click(deleteButton!);

    await waitFor(() =>
      expect(document.querySelector('.persona-prompt-label textarea')).toHaveValue('Default prompt'),
    );
    expect(client.sent).toContainEqual({
      method: 'DELETE',
      path: '/operator/profiles/profile-2',
      body: undefined,
    });
  });
});

describe('ModelsPanel', () => {
  it('loads non-secret channels, saves JSON, and runs a connectivity test', async () => {
    const payload = {
      source: 'runtime' as const,
      channels: [
        {
          name: 'primary',
          base_url: 'https://models.example/v1',
          api_key_env: 'MODEL_API_KEY',
          priority: 0,
          weight: 1,
          enabled: true,
          model_map: { chat: { model: 'chat-model' } },
        },
      ],
      usage: [
        {
          channel: 'primary',
          calls: 3,
          prompt_tokens: 100,
          completion_tokens: 20,
          cost_usd_micros: 80,
          last_status: 'success',
          last_called_at: '2026-07-27T00:00:00Z',
        },
      ],
      daily_usage: [
        {
          day: '2026-07-27',
          calls: 3,
          prompt_tokens: 100,
          completion_tokens: 20,
          cost_usd_micros: 80,
        },
      ],
      conversation_usage: [
        {
          conversation_id: 'conv-1',
          stable_key: 'v1:qq-main:DIRECT:10001:0',
          calls: 2,
          prompt_tokens: 80,
          completion_tokens: 10,
          cost_usd_micros: 40,
        },
      ],
    };
    const client = stubClient({
      '/operator/models/primary/test': {
        ok: true,
        channel: 'primary',
        model: 'chat-model',
        latency_ms: 12,
      },
      '/operator/models': payload,
    });

    render(<ModelsPanel client={client} />);

    await screen.findByText('primary');
    expect(screen.getAllByText('$0.000080').length).toBeGreaterThan(0);
    expect(screen.getByText('2026-07-27')).toBeInTheDocument();
    expect(screen.getByText('v1:qq-main:DIRECT:10001:0')).toBeInTheDocument();
    expect(screen.getByLabelText('模型渠道 JSON')).not.toHaveValue(
      expect.stringContaining('secret-key'),
    );

    fireEvent.click(screen.getByRole('button', { name: '保存渠道' }));
    await waitFor(() =>
      expect(client.sent[0]).toMatchObject({
        method: 'PUT',
        path: '/operator/models',
        body: { channels: payload.channels },
      }),
    );

    fireEvent.click(screen.getByRole('button', { name: '测试 primary' }));
    await screen.findByText('primary / chat-model / 12 ms');
  });
});
