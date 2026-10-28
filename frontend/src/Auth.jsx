import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { api, validateUser } from './api.js';
import { clearRecommendationRecovery } from './PortfolioRecommendations.jsx';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState({ status: 'checking', user: null, error: null });
  const revision = useRef(0);
  const changingSession = useRef(false);

  const restore = useCallback(async (signal) => {
    if (changingSession.current) return;
    const current = ++revision.current;
    try {
      const user = validateUser(await api('/auth/me', { signal }));
      if (!signal?.aborted && revision.current === current) {
        setSession({ status: 'authenticated', user, error: null });
      }
    } catch (error) {
      if (!signal?.aborted && revision.current === current) {
        if (error.status === 401) clearRecommendationRecovery();
        setSession({
          status: error.status === 401 ? 'anonymous' : 'error',
          user: null,
          error: error.status === 401 ? null : error.message,
        });
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    restore(controller.signal);
    return () => { controller.abort(); revision.current += 1; };
  }, [restore]);

  // Catch expiration or logout in another tab when the user returns to this tab.
  useEffect(() => {
    if (session.status !== 'authenticated') return;
    const controller = new AbortController();
    const onFocus = () => restore(controller.signal);
    window.addEventListener('focus', onFocus);
    return () => { controller.abort(); window.removeEventListener('focus', onFocus); };
  }, [session.status, restore]);

  const signIn = useCallback(async (login, password) => {
    changingSession.current = true;
    const current = ++revision.current;
    try {
      const user = validateUser(await api('/auth/login', { method: 'POST', body: { login, password } }));
      if (revision.current === current) {
        clearRecommendationRecovery();
        setSession({ status: 'authenticated', user, error: null });
      }
    } finally {
      changingSession.current = false;
    }
  }, []);

  const signOut = useCallback(async () => {
    changingSession.current = true;
    const current = ++revision.current;
    try {
      await api('/auth/logout', { method: 'POST' });
      if (revision.current === current) {
        clearRecommendationRecovery();
        setSession({ status: 'anonymous', user: null, error: null });
      }
    } finally {
      changingSession.current = false;
    }
  }, []);

  const request = useCallback(async (path, options) => {
    const current = revision.current;
    try {
      return await api(path, options);
    } catch (error) {
      if (error.status === 401 && revision.current === current) {
        revision.current += 1;
        clearRecommendationRecovery();
        setSession({ status: 'anonymous', user: null, error: null });
      }
      throw error;
    }
  }, []);

  const retry = () => {
    setSession({ status: 'checking', user: null, error: null });
    restore();
  };

  return (
    <AuthContext.Provider value={{ ...session, signIn, signOut, request, retry }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
