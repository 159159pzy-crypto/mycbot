import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorClient } from '../api';
import { ConversationsPanel } from './ConversationsPanel';
import { MemoriesPanel } from './MemoriesPanel';
import { OverviewPanel } from './OverviewPanel';
import { PersonaPanel } from './PersonaPanel';
import { PluginsPanel } from './PluginsPanel';

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
    send: <T,>(method: 'POST' | 'PUT', path: string, body?: unknown) => {
      sent.push({ method, path, body });
      return Promise.resolve(resolve(path) as T);
    },
  };
}

describe('OverviewPanel', () => {
  it('renders usage totals, sparkline, queues, and outcomes', async () => {
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
    expect(screen.getByText('750ms / 1900ms')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Token usage over 2 days/ })).toBeInTheDocument();
    expect(screen.getByText('outbound_dead_letter')).toBeInTheDocument();
    expect(screen.getByText('replied')).toBeInTheDocument();
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

    const row = await screen.findByRole('button', { name: /TELEGRAM \/ DIRECT \/ 777/ });
    fireEvent.click(row);

    await waitFor(() => expect(screen.getByText('讲个笑话')).toBeInTheDocument());
    expect(screen.getByText(/replied \/ DIRECT_MESSAGE/)).toBeInTheDocument();
    expect(screen.getByText(/web_search\(timeout\)/)).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }));

    await waitFor(() =>
      expect(screen.getByText('No memories match the current filters.')).toBeInTheDocument(),
    );
    expect(client.sent[0]).toMatchObject({
      method: 'POST',
      path: '/operator/memories/mem-1/revoke',
    });
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

    await screen.findByText('example.dice v1.0.0');
    expect(screen.getByText(/tools: roll_dice/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Approve tool id'), {
      target: { value: 'new_tool' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

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
  it('loads the persona and saves an edited prompt', async () => {
    const client = stubClient({
      '/operator/config/persona': { override: null, default: 'You are MyBot.' },
    });
    const sendSpy = vi.spyOn(client, 'send');

    render(<PersonaPanel client={client} />);

    const textarea = await screen.findByLabelText('System prompt');
    expect(textarea).toHaveValue('You are MyBot.');

    fireEvent.change(textarea, { target: { value: '你是高冷的猫娘助手。' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save persona' }));

    await waitFor(() => expect(screen.getByText('Persona saved.')).toBeInTheDocument());
    expect(sendSpy).toHaveBeenCalledWith('PUT', '/operator/config/persona', {
      system_prompt: '你是高冷的猫娘助手。',
    });
  });
});
