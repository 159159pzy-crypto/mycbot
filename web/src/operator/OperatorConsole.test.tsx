import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OperatorConsole } from './OperatorConsole';

const jsonResponse = (payload: unknown, status = 200) =>
  Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response);

describe('OperatorConsole', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function unlock(token = 's3cret') {
    fireEvent.change(screen.getByLabelText('Operator token'), {
      target: { value: token },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Unlock console' }));
  }

  it('renders the token gate and performs no fetch before unlocking', () => {
    render(<OperatorConsole />);

    expect(screen.getByLabelText('Operator token')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('unlocking loads the overview with the bearer token attached', async () => {
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

    render(<OperatorConsole />);
    unlock();

    await waitFor(() => expect(screen.getByText('420')).toBeInTheDocument());
    const requests = fetchMock.mock.calls.map(([input, init]) => ({
      url: String(input),
      auth: new Headers((init as RequestInit).headers).get('Authorization'),
    }));
    expect(requests.every((request) => request.auth === 'Bearer s3cret')).toBe(true);
    expect(screen.getByRole('button', { name: 'Memories' })).toBeInTheDocument();
  });

  it('a rejected token drops back to the gate with a notice', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ detail: 'nope' }, 401));

    render(<OperatorConsole />);
    unlock('wrong');

    await waitFor(() =>
      expect(
        screen.getByText('The operator token was rejected. Enter it again.'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByLabelText('Operator token')).toBeInTheDocument();
  });

  it('locking returns to the gate without fetching further', async () => {
    fetchMock.mockImplementation(() =>
      jsonResponse({
        usage: [],
        turns: { by_outcome: {}, avg_latency_ms: 0, p95_latency_ms: 0 },
        queues: {},
      }),
    );

    render(<OperatorConsole />);
    unlock();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Lock' })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Lock' }));

    expect(screen.getByLabelText('Operator token')).toBeInTheDocument();
  });
});
