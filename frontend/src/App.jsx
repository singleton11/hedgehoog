import { useState } from 'react';
import { AuthProvider, useAuth } from './Auth.jsx';
import InstrumentWorkspace from './InstrumentWorkspace.jsx';
import QuotaBudget from './QuotaBudget.jsx';
import Icon, { BrandMark } from './Icon.jsx';

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
    <main className="login-page">
      <div className="login-story">
        <div className="login-brand"><BrandMark />Hedgehoog</div>
        <div className="login-story-copy">
          <p className="eyebrow">A clearer perspective</p>
          <h2>Your future.<br /> In focus.</h2>
          <p>Explore ETFs, understand the numbers, and plan your next chapter.</p>
          <div className="login-art" aria-hidden="true">
            <div className="art-orbit" /><div className="art-orbit art-orbit-inner" />
            <svg viewBox="0 0 460 180" fill="none" focusable="false">
              <path d="M0 155C55 155 45 122 100 128S160 135 198 98 241 130 280 77 334 87 367 40 415 55 460 8" stroke="currentColor" strokeWidth="3" />
              <path d="M0 177C70 166 84 180 130 151S198 164 245 123 300 144 348 101 408 116 460 71" stroke="currentColor" strokeOpacity=".25" strokeWidth="2" />
            </svg>
          </div>
        </div>
        <p className="login-story-footer"><Icon name="layers" />Data. Perspective. Possibility.</p>
      </div>
      <div className="login-panel">
        <p className="eyebrow">Your investment workspace</p>
        <h1>Sign in</h1>
        <p className="muted">Good to see you. Let’s pick up where you left off.</p>
        <form onSubmit={submit}>
          <label htmlFor="login">Login</label>
          <input id="login" name="username" autoComplete="username" value={login}
            onChange={(event) => setLogin(event.target.value)} required maxLength={128} disabled={busy} />
          <label htmlFor="password">Password</label>
          <input id="password" name="password" type="password" autoComplete="current-password"
            value={password} onChange={(event) => setPassword(event.target.value)}
            required maxLength={1024} disabled={busy} />
          {error && <p role="alert">{error}</p>}
          <button type="submit" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}<Icon name="arrow" /></button>
        </form>
        <p className="login-security"><Icon name="shield" />Your workspace, securely connected.</p>
      </div>
    </main>
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
    <main className="workspace" id="workspace">
      <a className="skip-link" href="#workspace-content" onClick={(event) => {
        event.preventDefault();
        document.getElementById('workspace-content')?.focus();
      }}>Skip to content</a>
      <header className="app-header">
        <h1 className="brand"><a href="#/"><BrandMark />Hedgehoog</a></h1>
        <span className="workspace-label">Investment workspace</span>
        <QuotaBudget />
        <div className="account-controls">
          <span className="account-avatar" aria-hidden="true">{user.login.slice(0, 1).toUpperCase()}</span>
          <span className="account-name">{user.login}</span>
          <button className="secondary sign-out" onClick={logout} disabled={busy}><Icon name="logout" />{busy ? 'Signing out…' : 'Sign out'}</button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <InstrumentWorkspace />
    </main>
  );
}

function AuthorizedApp() {
  const { status, error, retry } = useAuth();
  if (status === 'checking') return <main className="session-card"><BrandMark /><p role="status">Checking your session…</p></main>;
  if (status === 'error') {
    return <main className="session-card"><BrandMark /><h1>Hedgehoog</h1><p role="alert">{error}</p><p>Start the app with <code>make run</code>, then try again.</p><button onClick={retry}>Try again</button></main>;
  }
  return status === 'authenticated' ? <Dashboard /> : <Login />;
}

export default function App() {
  return <AuthProvider><AuthorizedApp /></AuthProvider>;
}
