import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

const responseWithUnknown = (payload: unknown) =>
  Promise.resolve({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response);

const dependency = (name: '数据库依赖' | 'Redis 依赖') =>
  screen.getByRole('region', { name });

const POLL_INTERVAL_MS = 30_000;
const ERROR_RETRY_MS = 5_000;
const FETCH_TIMEOUT_MS = 5_000;
const STALE_AFTER_MS = 60_000;

async function flushRequest() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function unlock() {
  fireEvent.change(screen.getByLabelText('操作员令牌'), {
    target: { value: 's3cret' },
  });
  fireEvent.click(screen.getByRole('button', { name: '解锁控制台' }));
}

describe('App', () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it('gates the console behind the operator token', () => {
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);

    expect(screen.getByLabelText('操作员令牌')).toBeInTheDocument();
    expect(screen.queryByRole('main')).not.toBeInTheDocument();
  });

  it('provides an accessible console structure after unlocking', () => {
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);
    unlock();

    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: '控制台导航' })).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 1, name: '系统状态' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite');
  });

  it('announces loading while requesting the readiness endpoint', () => {
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);
    unlock();

    expect(screen.getByRole('status')).toHaveTextContent('正在建立遥测链路');
    expect(within(dependency('数据库依赖')).getByText('等待信号')).toBeInTheDocument();
    expect(within(dependency('Redis 依赖')).getByText('等待信号')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/health/ready');
  });

  it('renders confirmed database and Redis health', async () => {
    fetchMock.mockReturnValueOnce(responseWith(healthyPayload));

    render(<App />);
    unlock();

    expect(await screen.findByText('一切正常')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('一切正常');
    expect(within(dependency('数据库依赖')).getByText('在线')).toBeInTheDocument();
    expect(within(dependency('Redis 依赖')).getByText('在线')).toBeInTheDocument();
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
    unlock();

    expect(await screen.findByText('服务已降级')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('服务已降级');
    expect(within(dependency('数据库依赖')).getByText('离线')).toBeInTheDocument();
    expect(
      within(dependency('数据库依赖')).getByText(/Probe timed out/),
    ).toBeInTheDocument();
    expect(within(dependency('Redis 依赖')).getByText('在线')).toBeInTheDocument();
  });

  it('announces a network error without reporting stale health', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Network request failed'));

    render(<App />);
    unlock();

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('无法获取就绪信号');
    });
    expect(within(dependency('数据库依赖')).getByText('未知')).toBeInTheDocument();
    expect(within(dependency('Redis 依赖')).getByText('未知')).toBeInTheDocument();
  });

  it.each([
    {
      label: 'non-string dependency detail',
      payload: {
        status: 'ready',
        dependencies: {
          database: { status: 'up', detail: 42 },
          redis: { status: 'up' },
        },
      },
    },
    {
      label: 'aggregate status inconsistent with dependencies',
      payload: {
        status: 'ready',
        dependencies: {
          database: { status: 'down', detail: 'TimeoutError' },
          redis: { status: 'up' },
        },
      },
    },
  ])('rejects malformed readiness payload: $label', async ({ payload }) => {
    fetchMock.mockReturnValueOnce(responseWithUnknown(payload));

    render(<App />);
    unlock();

    expect(await screen.findByText('无法获取就绪信号')).toBeInTheDocument();
  });

  it('bounds a readiness request that never settles', async () => {
    vi.useFakeTimers();
    fetchMock.mockReturnValueOnce(new Promise<Response>(() => undefined));

    render(<App />);
    unlock();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FETCH_TIMEOUT_MS);
    });

    expect(screen.getByRole('status')).toHaveTextContent('无法获取就绪信号');
  });

  it('updates from healthy to degraded on the next poll', async () => {
    vi.useFakeTimers();
    fetchMock
      .mockReturnValueOnce(responseWith(healthyPayload))
      .mockReturnValueOnce(
        responseWith(
          {
            status: 'not_ready',
            dependencies: {
              database: { status: 'down', detail: 'TimeoutError' },
              redis: { status: 'up' },
            },
          },
          503,
        ),
      );

    render(<App />);
    unlock();
    await flushRequest();
    expect(screen.getByRole('status')).toHaveTextContent('一切正常');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(screen.getByRole('status')).toHaveTextContent('服务已降级');
    expect(within(dependency('数据库依赖')).getByText('离线')).toBeInTheDocument();
  });

  it('recovers from an initial network error on backoff retry', async () => {
    vi.useFakeTimers();
    fetchMock
      .mockRejectedValueOnce(new TypeError('Network request failed'))
      .mockReturnValueOnce(responseWith(healthyPayload));

    render(<App />);
    unlock();
    await flushRequest();
    expect(screen.getByRole('status')).toHaveTextContent('无法获取就绪信号');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ERROR_RETRY_MS);
    });

    expect(screen.getByRole('status')).toHaveTextContent('一切正常');
  });

  it('pauses polling while hidden and refreshes when visible again', async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    fetchMock
      .mockReturnValueOnce(responseWith(healthyPayload))
      .mockReturnValueOnce(responseWith(healthyPayload));

    render(<App />);
    await flushRequest();
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    await flushRequest();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('announces when the last successful readiness data becomes stale', async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    fetchMock.mockReturnValueOnce(responseWith(healthyPayload));

    render(<App />);
    unlock();
    await flushRequest();
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STALE_AFTER_MS);
    });

    expect(screen.getByRole('status')).toHaveTextContent('就绪数据已过期');
  });
});
