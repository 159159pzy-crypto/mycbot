import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App';

type ReadinessPayload = {
  status: 'ready' | 'not_ready';
  dependencies: {
    database: {
      status: 'up' | 'down';
      detail?: string;
    };
    redis: {
      status: 'up' | 'down';
      detail?: string;
    };
  };
};

const healthyPayload: ReadinessPayload = {
  status: 'ready',
  dependencies: {
    database: { status: 'up' },
    redis: { status: 'up' },
  },
};

const responseWith = (payload: ReadinessPayload, status = 200) =>
  Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response);

const dependency = (name: 'Database' | 'Redis') =>
  screen.getByRole('listitem', { name: `${name} dependency` });

describe('App', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it('provides an accessible operations page structure', () => {
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);

    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 1, name: /operations readiness/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByRole('contentinfo')).toBeInTheDocument();
  });

  it('announces loading while requesting the readiness endpoint', () => {
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);

    expect(screen.getByRole('status')).toHaveTextContent(
      /establishing telemetry link/i,
    );
    expect(within(dependency('Database')).getByText(/awaiting signal/i)).toBeInTheDocument();
    expect(within(dependency('Redis')).getByText(/awaiting signal/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/health/ready');
  });

  it('renders confirmed database and Redis health', async () => {
    fetchMock.mockReturnValueOnce(responseWith(healthyPayload));

    render(<App />);

    expect(await screen.findByText(/all systems ready/i)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/all systems ready/i);
    expect(within(dependency('Database')).getByText('Online')).toBeInTheDocument();
    expect(within(dependency('Redis')).getByText('Online')).toBeInTheDocument();
  });

  it('renders dependency detail from a degraded readiness response', async () => {
    fetchMock.mockReturnValueOnce(
      responseWith(
        {
          status: 'not_ready',
          dependencies: {
            database: { status: 'down', detail: 'Probe timed out' },
            redis: { status: 'up' },
          },
        },
        503,
      ),
    );

    render(<App />);

    expect(await screen.findByText(/readiness degraded/i)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(/readiness degraded/i);
    expect(within(dependency('Database')).getByText('Offline')).toBeInTheDocument();
    expect(within(dependency('Database')).getByText('Probe timed out')).toBeInTheDocument();
    expect(within(dependency('Redis')).getByText('Online')).toBeInTheDocument();
  });

  it('announces a network error without reporting stale health', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Network request failed'));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(
        /readiness signal unavailable/i,
      );
    });
    expect(within(dependency('Database')).getByText('Unknown')).toBeInTheDocument();
    expect(within(dependency('Redis')).getByText('Unknown')).toBeInTheDocument();
  });
});
