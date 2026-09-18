import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
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
    if (url.startsWith('/api/instruments/acquired?')) return response(200, { items: [], total: 0, limit: 20, offset: 0 });
    if (url === '/api/') return response(200, { message: 'Hello, World!' });
    if (url === '/api/admin') return response(200, { message: 'Administrator access granted' });
    throw new Error(`Unexpected URL ${url}`);
  });
  vi.stubGlobal('fetch', fetch);
  return { fetch, expire: () => { user = null; } };
}

describe('authentication', () => {
  it('does not render protected content while checking the session', async () => {
    let resolve;
    vi.stubGlobal('fetch', vi.fn(() => new Promise((done) => { resolve = done; })));
    render(<App />);
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session');
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
    await act(async () => resolve(response(401, {})));
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('shows login to anonymous visitors without fetching protected resources', async () => {
    const server = mockServer();
    render(<App />);
    expect(await screen.findByLabelText('Login')).toBeInTheDocument();
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
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
    expect(await screen.findByText('Administrator access granted')).toBeInTheDocument();
    expect(server.fetch).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({
      credentials: 'same-origin', method: 'POST', body: JSON.stringify({ login: 'admin', password: 'admin' }),
    }));
    expect(storageWrite).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
  });

  it('restores the authenticated session on page load', async () => {
    mockServer(admin);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    expect(await screen.findByText('Hello, World!')).toBeInTheDocument();
  });

  it('does not render or request the admin section for a regular user', async () => {
    const server = mockServer({ ...admin, login: 'alice', role: 'user' });
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.queryByText('Administration')).not.toBeInTheDocument();
    expect(server.fetch.mock.calls.some(([url]) => url === '/api/admin')).toBe(false);
  });

  it('returns to login when a protected API request responds 401', async () => {
    const server = mockServer(admin);
    server.fetch.mockImplementation(async (url) => url === '/api/auth/me' ? response(200, admin) : response(401, {}));
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
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
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('does not treat a malformed session response as authorized', async () => {
    const server = mockServer();
    server.fetch.mockResolvedValueOnce(response(200, { login: 'admin' }));
    render(<App />);
    expect(await screen.findByRole('alert')).toHaveTextContent('unexpected user');
    expect(screen.queryByText('Backend response')).not.toBeInTheDocument();
  });

  it('does not let a focus check override a pending logout', async () => {
    const server = mockServer(admin);
    render(<App />);
    await screen.findByText('Administrator access granted');
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
    await screen.findByText('Administrator access granted');
    server.fetch.mockResolvedValueOnce(response(503, { detail: 'Service unavailable' }));
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    expect(screen.getByRole('heading', { name: 'Hedgehoog' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign out' })).toBeEnabled());
  });
});
