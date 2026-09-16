export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, { body, ...options } = {}) {
  let response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('Could not reach the backend. Please try again.', 0);
  }

  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      typeof data?.detail === 'string' ? data.detail : `Backend returned HTTP ${response.status}.`,
      response.status,
    );
  }
  if (response.status !== 204 && data === null) {
    throw new ApiError('Backend returned an unexpected response.', response.status);
  }
  return data;
}

export function validateUser(user) {
  if (typeof user?.id !== 'string' || typeof user?.login !== 'string'
      || !['admin', 'user'].includes(user?.role)) {
    throw new ApiError('Backend returned an unexpected user.', 200);
  }
  return user;
}
