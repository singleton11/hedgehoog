import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.jsx';

const admin = { id: 'user-id', login: 'admin', role: 'admin' };
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });

function mockServer(initialUser = null) {
  let user = initialUser;
  const fetch = vi.fn(async (url, options) => {
    if (url === '/api/auth/me') return user ? response(200, user) : response(401, { detail: 'Authentication required' });
    if (url === '/api/auth/login') {
      const credentials = JSON.parse(options.body);
      if (credentials.login !== 'admin' || credentials.password !== 'admin') return response(401, { detail: 'Invalid login or password' });
      user = admin;
      return response(200, user);
    }
    if (url === '/api/auth/logout') { user = null; return response(204); }
    if (!user) return response(401, { detail: 'Authentication required' });
    if (url === '/api/instruments/budget') return response(200, { provider: 'yfinance', metered: false, month: '2026-09', used: 0, limit: null, remaining: null });
    if (url.startsWith('/api/instruments/acquired?')) return response(200, { items: [], total: 0, limit: 20, offset: 0 });
    throw new Error(`Unexpected URL ${url}`);
  });
  vi.stubGlobal('fetch', fetch);
  return { fetch, expire: () => { user = null; } };
}

describe('logo navigation', () => {
  afterEach(() => window.history.replaceState(null, '', '/'));

  it('returns to the ETF list when the logo is clicked', async () => {
    window.history.replaceState(null, '', '/#/goal');
    mockServer(admin);
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Customers', level: 2 })).toBeInTheDocument();
    const logo = screen.getByRole('link', { name: 'Hedgehoog' });
    expect(logo).toHaveAttribute('href', '#/');
    const header = logo.closest('header');
    const budget = within(header).getByRole('group', { name: 'Provider status' });
    await waitFor(() => expect(budget).toHaveTextContent('Yahoo Finance · experimental'));
    expect(within(header).getByText('admin')).toBeInTheDocument();

    await user.click(logo);

    expect(await screen.findByText('No acquired ETFs yet')).toBeInTheDocument();
    expect(window.location.hash).toBe('#/');
    expect(screen.getByRole('group', { name: 'Provider status' })).toBe(budget);
    expect(screen.queryByRole('heading', { name: 'Customers', level: 2 })).not.toBeInTheDocument();
  });
});

describe('workspace presentation', () => {
  afterEach(() => window.history.replaceState(null, '', '/'));

  it('lets keyboard users skip navigation without changing the route', async () => {
    mockServer(admin);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    await user.tab();
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(document.getElementById('workspace-content')).toHaveFocus();
    expect(window.location.hash).toBe('');
  });

  it('expands the mobile menu, closes on Escape and restores focus to its toggle', async () => {
    mockServer(admin);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    const toggle = screen.getByRole('button', { name: 'Menu' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'workspace-navigation-links');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    screen.getByRole('link', { name: 'Settings' }).focus();
    await user.keyboard('{Escape}');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveFocus();
  });

  it('closes the menu on navigation, including selecting the current page', async () => {
    mockServer(admin);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    const toggle = screen.getByRole('button', { name: 'Menu' });
    await user.click(toggle);
    await user.click(screen.getByRole('link', { name: 'ETFs', exact: true }));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(document.getElementById('workspace-content')).toHaveFocus();
    await user.click(toggle);
    await user.click(screen.getByRole('link', { name: 'Customers', exact: true }));
    expect(await screen.findByRole('heading', { name: 'Customers', level: 2 })).toBeInTheDocument();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('link', { name: 'Customers' })).toHaveAttribute('aria-current', 'page');
    await user.click(toggle);
    await act(async () => {
      window.location.hash = '/';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('shows the ETF table without redundant summary cards', async () => {
    const server = mockServer(admin);
    const fallback = server.fetch.getMockImplementation();
    server.fetch.mockImplementation((url, options) => {
      if (url.startsWith('/api/instruments/acquired?')) return Promise.resolve(response(200, {
        total: 42, limit: 20, offset: 0, items: [
          { instrument_id: 'IE00B4L5Y983', isin: 'IE00B4L5Y983', name: 'World ETF', ticker: 'WORLD', quote_count: 1200 },
          { instrument_id: 'IE00B3XXRP09', isin: 'IE00B3XXRP09', name: 'Profile-only ETF', quote_count: 0 },
        ],
      }));
      return fallback(url, options);
    });
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Acquired ETFs (42)' })).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.queryByLabelText('Collection overview')).not.toBeInTheDocument();
    expect(screen.queryByText('With price history')).not.toBeInTheDocument();
    expect(screen.queryByText('Saved daily quotes')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'World ETF' })).toHaveAttribute('href', '#/instruments/IE00B4L5Y983');
    expect(server.fetch.mock.calls.every(([, options]) => !options.method || options.method === 'GET')).toBe(true);
  });

  it('does not show empty summary cards before any ETF has been acquired', async () => {
    mockServer(admin);
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    expect(screen.queryByLabelText('Collection overview')).not.toBeInTheDocument();
  });
});

describe('ETF removal', () => {
  afterEach(() => window.history.replaceState(null, '', '/'));

  function removalServer({ paginated = false, fail = false } = {}) {
    const server = mockServer(admin);
    const fallback = server.fetch.getMockImplementation();
    let removed = false;
    server.fetch.mockImplementation(async (url, options) => {
      if (options.method === 'DELETE') {
        if (fail) return response(409, { detail: 'This ETF has an active acquisition. Retry later.' });
        removed = true;
        return response(204);
      }
      if (url.startsWith('/api/instruments/acquired?')) {
        const offset = Number(new URL(url, 'http://localhost').searchParams.get('offset'));
        const total = paginated ? (removed ? 20 : 21) : (removed ? 0 : 1);
        return response(200, { total, limit: 20, offset, items: offset >= total ? [] : [{
          instrument_id: 'IE00B4L5Y983', isin: 'IE00B4L5Y983', name: 'World ETF', quote_count: 10,
        }] });
      }
      return fallback(url, options);
    });
    return server;
  }

  it('confirms deletion, permits cancellation, and refreshes the empty list', async () => {
    const server = removalServer();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Remove World ETF' }));
    expect(screen.getByRole('form', { name: 'Confirm ETF removal' })).toHaveTextContent('for all users');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(server.fetch.mock.calls.some(([, options]) => options.method === 'DELETE')).toBe(false);
    expect(screen.getByRole('button', { name: 'Remove World ETF' })).toHaveFocus();
    await user.click(screen.getByRole('button', { name: 'Remove World ETF' }));
    await user.click(screen.getByRole('button', { name: 'Delete saved data' }));
    expect(await screen.findByText('No acquired ETFs yet')).toBeInTheDocument();
    expect(screen.getByText('World ETF removed. Saved market data deleted.')).toBeInTheDocument();
    expect(server.fetch.mock.calls.filter(([, options]) => options.method === 'DELETE')).toHaveLength(1);
  });

  it('keeps the ETF and shows a retryable deletion failure', async () => {
    removalServer({ fail: true });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Remove World ETF' }));
    await user.click(screen.getByRole('button', { name: 'Delete saved data' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('active acquisition');
    expect(screen.getByRole('link', { name: 'World ETF' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete saved data' })).toBeEnabled();
  });

  it('returns to the previous page when the last page becomes empty', async () => {
    const server = removalServer({ paginated: true });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Next ETFs' }));
    await screen.findByText('21–21 of 21 ETFs');
    await user.click(screen.getByRole('button', { name: 'Remove World ETF' }));
    await user.click(screen.getByRole('button', { name: 'Delete saved data' }));
    await screen.findByText('1–1 of 20 ETFs');
    expect(screen.getByRole('button', { name: 'Previous ETFs' })).toBeDisabled();
    expect(server.fetch.mock.calls.at(-1)[0]).toContain('offset=0');
  });
});

describe('authentication', () => {
  it('does not render protected content while checking the session', async () => {
    let resolve;
    vi.stubGlobal('fetch', vi.fn(() => new Promise((done) => { resolve = done; })));
    render(<App />);
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    await act(async () => resolve(response(401, {})));
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('shows login to anonymous visitors without fetching protected resources', async () => {
    const server = mockServer();
    render(<App />);
    expect(await screen.findByLabelText('Login')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    expect(server.fetch).toHaveBeenCalledTimes(1);
  });

  it('rejects bad credentials, logs in, and logs out', async () => {
    const storageWrite = vi.fn();
    vi.stubGlobal('localStorage', { setItem: storageWrite });
    vi.stubGlobal('sessionStorage', { setItem: storageWrite });
    const server = mockServer();
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText('Login'), 'admin');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid login or password');
    expect(screen.getByLabelText('Password')).toHaveValue('');
    await user.type(screen.getByLabelText('Password'), 'admin');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.queryByText(/Welcome to your workspace/)).not.toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Add ETF' })).toHaveAttribute('href', '#/add');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    expect(server.fetch).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({
      credentials: 'same-origin', method: 'POST', body: JSON.stringify({ login: 'admin', password: 'admin' }),
    }));
    expect(storageWrite).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
  });

  it('restores the authenticated session on page load', async () => {
    mockServer(admin);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Add ETF' })).toHaveAttribute('href', '#/add');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
  });

  it.each([admin, { ...admin, login: 'alice', role: 'user' }])('does not render or fetch service status for $role', async (user) => {
    const server = mockServer(user);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    await screen.findByText('No acquired ETFs yet');
    expect(screen.getByText(user.login)).toBeInTheDocument();
    expect(screen.queryByText('Service status')).not.toBeInTheDocument();
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
    expect(screen.queryByText('Administration')).not.toBeInTheDocument();
    expect(server.fetch.mock.calls.some(([url]) => url === '/api/' || url === '/api/admin')).toBe(false);
  });

  it('returns to login when a protected API request responds 401', async () => {
    const server = mockServer(admin);
    server.fetch.mockImplementation(async (url) => url === '/api/auth/me' ? response(200, admin) : response(401, {}));
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
  });

  it('checks for expired sessions when the window gains focus', async () => {
    const server = mockServer(admin);
    render(<App />);
    await screen.findByRole('heading', { name: 'Hedgehoog' });
    server.expire();
    fireEvent.focus(window);
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('shows an outage with retry rather than authenticated content', async () => {
    const server = mockServer();
    server.fetch.mockRejectedValueOnce(new Error('offline'));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the backend');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('does not treat a malformed session response as authorized', async () => {
    const server = mockServer();
    server.fetch.mockResolvedValueOnce(response(200, { login: 'admin' }));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('unexpected user');
    expect(screen.queryByRole('combobox', { name: 'Find an ETF' })).not.toBeInTheDocument();
  });

  it('does not let a focus check override a pending logout', async () => {
    const server = mockServer(admin);
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    let finishLogout;
    server.fetch.mockImplementationOnce(() => new Promise((resolve) => { finishLogout = resolve; }));
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    const callsBeforeFocus = server.fetch.mock.calls.length;
    fireEvent.focus(window);
    expect(server.fetch).toHaveBeenCalledTimes(callsBeforeFocus);
    await act(async () => finishLogout(response(204)));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('reports failed logout without pretending the cookie was cleared', async () => {
    const server = mockServer(admin);
    render(<App />);
    await screen.findByText('No acquired ETFs yet');
    server.fetch.mockResolvedValueOnce(response(503, { detail: 'Service unavailable' }));
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    expect(screen.getByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign out' })).toBeEnabled());
  });
});
