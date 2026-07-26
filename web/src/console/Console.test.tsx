import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HealthState } from '../health/types';
import { Console } from './Console';

const loadingHealth: HealthState = { kind: 'loading' };

const jsonResponse = (payload: unknown, status = 200) =>
  Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response);

describe('Console', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function unlock(token = 's3cret') {
    fireEvent.change(screen.getByLabelText('操作员令牌'), {
      target: { value: token },
    });
    fireEvent.click(screen.getByRole('button', { name: '解锁控制台' }));
  }

  it('renders the token gate and performs no fetch before unlocking', () => {
    render(<Console health={loadingHealth} />);

    expect(screen.getByLabelText('操作员令牌')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('unlocking opens the status view without operator fetches', () => {
    render(<Console health={loadingHealth} />);
    unlock();

    expect(
      screen.getByRole('heading', { level: 1, name: '系统状态' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '记忆' })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('navigating to the overview loads it with the bearer token attached', async () => {
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.includes('/operator/usage')) {
        return jsonResponse({ usage: [{ day: '2026-07-26', turns: 3, tokens: 420 }] });
      }
      return jsonResponse({
        turns: { by_outcome: { replied: 3 }, avg_latency_ms: 800, p95_latency_ms: 1200 },
        queues: { ingest: 0, outbound: 1, ingest_dead_letter: 0, outbound_dead_letter: 0 },
      });
    });

    render(<Console health={loadingHealth} />);
    unlock();
    fireEvent.click(screen.getByRole('button', { name: '数据总览' }));

    await waitFor(() => expect(screen.getByText('420')).toBeInTheDocument());
    const requests = fetchMock.mock.calls.map(([input, init]) => ({
      url: String(input),
      auth: new Headers((init as RequestInit).headers).get('Authorization'),
    }));
    expect(requests.length).toBeGreaterThan(0);
    expect(requests.every((request) => request.auth === 'Bearer s3cret')).toBe(true);
  });

  it('a rejected token drops back to the gate with a notice', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ detail: 'nope' }, 401));

    render(<Console health={loadingHealth} />);
    unlock('wrong');
    fireEvent.click(screen.getByRole('button', { name: '数据总览' }));

    await waitFor(() =>
      expect(
        screen.getByText('操作员令牌被拒绝，请重新输入。'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByLabelText('操作员令牌')).toBeInTheDocument();
  });

  it('locking returns to the gate without fetching further', () => {
    render(<Console health={loadingHealth} />);
    unlock();

    fireEvent.click(screen.getByRole('button', { name: '锁定控制台' }));

    expect(screen.getByLabelText('操作员令牌')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
