import { useState } from 'react';
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
