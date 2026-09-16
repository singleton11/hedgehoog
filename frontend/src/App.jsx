import { useEffect, useState } from 'react';

export default function App() {
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const controller = new AbortController();

    async function fetchGreeting() {
      try {
        const response = await fetch('/api/', { signal: controller.signal });
        if (!response.ok) {
          throw new Error(`Backend returned HTTP ${response.status}.`);
        }

        const data = await response.json();
        if (typeof data.message !== 'string') {
          throw new Error('Backend returned an unexpected response.');
        }

        if (!controller.signal.aborted) {
          setMessage(data.message);
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setError(error.message);
        }
      }
    }

    fetchGreeting();
    return () => controller.abort();
  }, []);

  return (
    <main>
      <h1>Hello, World!</h1>
      <p>Welcome to Hedgehoog.</p>
      <section aria-labelledby="backend-heading">
        <h2 id="backend-heading">Backend response</h2>
        {error ? (
          <div role="alert">
            <p>Could not reach the backend: {error}</p>
            <p>Start it with <code>make run</code>, then refresh this page.</p>
          </div>
        ) : (
          <p role="status">{message ?? 'Loading greeting from the backend…'}</p>
        )}
      </section>
    </main>
  );
}
