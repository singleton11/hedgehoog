import { useEffect, useState } from 'react';
import { AuthProvider, useAuth } from './Auth.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';

function Login() {
  const { signIn } = useAuth();
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(login, password);
    } catch (error) {
      setError(error.message);
      setPassword('');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <p className="eyebrow">Hedgehoog</p>
      <h1>Sign in</h1>
      <form onSubmit={submit}>
        <label htmlFor="login">Login</label>
        <input id="login" name="username" autoComplete="username" value={login}
          onChange={(event) => setLogin(event.target.value)} required maxLength={128} disabled={busy} />
        <label htmlFor="password">Password</label>
        <input id="password" name="password" type="password" autoComplete="current-password"
          value={password} onChange={(event) => setPassword(event.target.value)}
          required maxLength={1024} disabled={busy} />
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </main>
  );
}

function BackendMessage({ path, heading }) {
  const { request } = useAuth();
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setMessage(null);
    setError(null);
    async function load() {
      try {
        const data = await request(path, { signal: controller.signal });
        if (typeof data?.message !== 'string') throw new Error('Backend returned an unexpected response.');
        if (!controller.signal.aborted) setMessage(data.message);
      } catch (error) {
        if (!controller.signal.aborted) setError(error.message);
      }
    }
    load();
    return () => controller.abort();
  }, [path, request, attempt]);

  return (
    <section aria-label={heading}>
      <h2>{heading}</h2>
      {error ? <><p role="alert">{error}</p><button className="secondary" onClick={() => setAttempt(attempt + 1)}>Retry</button></>
        : <p role="status">{message ?? 'Loading…'}</p>}
    </section>
  );
}

function Dashboard() {
  const { user, signOut } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function logout() {
    setBusy(true);
    setError(null);
    try {
      await signOut();
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="workspace">
      <header>
        <h1 className="brand">Hedgehoog</h1>
        <div className="account-controls"><span className="muted">{user.login}</span>
          <button className="secondary" onClick={logout} disabled={busy}>{busy ? 'Signing out…' : 'Sign out'}</button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <InstrumentWorkspace />
      <details className="service-status"><summary>Service status</summary>
        <BackendMessage path="/" heading="Backend response" />
        {user.role === 'admin' && <BackendMessage path="/admin" heading="Administration" />}
      </details>
    </main>
  );
}

function AuthorizedApp() {
  const { status, error, retry } = useAuth();
  if (status === 'checking') return <main><p role="status">Checking your session…</p></main>;
  if (status === 'error') {
    return <main><h1>Hedgehoog</h1><p role="alert">{error}</p><p>Start the app with <code>make run</code>, then try again.</p><button onClick={retry}>Try again</button></main>;
  }
  return status === 'authenticated' ? <Dashboard /> : <Login />;
}

export default function App() {
  return <AuthProvider><AuthorizedApp /></AuthProvider>;
}
