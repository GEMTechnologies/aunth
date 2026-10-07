import axios, { AxiosInstance } from 'axios';
import { AUTH_URL } from '../config';

/**
 * Auth transport.
 *
 * Two defects in this file were found by dumping the live route table from the
 * FastAPI app rather than reading this file's own comments, which described
 * endpoints that do not exist:
 *
 *   * `API_BASE_URL` was never imported, so registration threw ReferenceError
 *     before a request was ever made;
 *   * `/me`, `/auth/register-with-intent` and `POST /auth/oauth/{p}/callback`
 *     have never existed. The real paths are `/users/me` and
 *     `POST /auth/oauth/exchange`.
 *
 * Credential storage was also wrong. Tokens sat in localStorage, which any
 * script on the origin can read, and the OAuth callback read them out of the
 * URL query string, where they land in browser history, the Referer header,
 * and every proxy log between the provider and the browser.
 *
 * Now: the access token is a module variable reachable only by this module,
 * and the refresh token is either an HttpOnly cookie or -- when the backend is
 * configured for header delivery -- also only a module variable. Nothing
 * browser-persisted holds a credential.
 *
 * The cost is that a full page reload drops the session, so `restoreSession()`
 * re-establishes it from the cookie. That works whenever the backend runs with
 * REFRESH_TOKEN_DELIVERY=cookie, which is what production must use; see
 * docs/SECURITY_REMEDIATION.md.
 */

/**
 * The user shape the UI consumes.
 *
 * `display_name` is a required string here, but the backend's UserResponse
 * types it as Optional[str]. Rather than loosen the five component interfaces
 * that declare their own `User`, the null is normalised once at this boundary.
 * The alternative spreads `| null` into every render site and buys nothing:
 * the UI has always rendered an empty name in that case and still does.
 */
export interface AuthUser {
  id: string;
  display_name: string;
  avatar_url?: string;
  locale: string;
  created_at?: string;
  status: string;
  primary_email?: {
    id?: string;
    email: string;
    is_verified: boolean;
  };
}

function toAuthUser(data: Record<string, unknown>): AuthUser {
  const raw = data as Record<string, unknown>;
  return {
    ...(raw as unknown as AuthUser),
    display_name: (raw.display_name as string | null) ?? '',
    // Optional-but-nullable on the wire, optional-but-undefined in the UI:
    // `src={null}` and an explicit undefined mean the same thing to React, but
    // only one of them satisfies the components' prop types.
    avatar_url: (raw.avatar_url as string | null) ?? undefined,
    primary_email: (raw.primary_email as AuthUser['primary_email']) ?? undefined,
  };
}

export interface AuthResponse {
  access_token: string;
  /** Present only in header-delivery mode; null when the cookie carries it. */
  refresh_token?: string | null;
  token_type: string;
  expires_in: number;
  user?: AuthUser;
}

// --- Credential store -------------------------------------------------------
// Deliberately not localStorage or sessionStorage. A module variable is
// reachable only through code on this page, and is discarded on reload.

let accessToken: string | null = null;
let refreshToken: string | null = null;

/**
 * Whether the backend hands out refresh tokens in a cookie or a body.
 * Learned from the first token response rather than configured, so the two
 * halves cannot disagree: a mismatched frontend would simply get 401 on every
 * refresh.
 */
let delivery: 'unknown' | 'body' | 'cookie' = 'unknown';

export function getAccessToken(): string | null {
  return accessToken;
}

function adoptTokens(data: {
  access_token?: string;
  refresh_token?: string | null;
}): void {
  if (data.access_token) accessToken = data.access_token;
  if (data.refresh_token) {
    refreshToken = data.refresh_token;
    delivery = 'body';
  } else if (delivery === 'unknown') {
    // No body token on a token-bearing response means the credential was
    // delivered some other way. The only other way this service has is a cookie.
    delivery = 'cookie';
  }
}

/** Drop every credential this module holds. */
function forgetTokens(): void {
  accessToken = null;
  refreshToken = null;
}

/**
 * Re-establish a session after a page reload, using the HttpOnly cookie.
 * Resolves to null when there is no usable session, which is the normal case
 * for a visitor and must not be logged as an error.
 */
export async function restoreSession(): Promise<AuthResponse | null> {
  try {
    const response = await axios.post(
      `${AUTH_URL}/api/v1/auth/refresh`,
      {},
      { withCredentials: true, timeout: 10000 }
    );
    adoptTokens(response.data);
    return response.data as AuthResponse;
  } catch {
    forgetTokens();
    return null;
  }
}

// Create axios instance
const api: AxiosInstance = axios.create({
  baseURL: `${AUTH_URL}/api/v1`,
  timeout: 10000,
  // Required for the refresh cookie to be sent, and for Set-Cookie on login to
  // be stored, whenever the API is on a different origin from the SPA.
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

api.interceptors.request.use(
  (config) => {
    if (accessToken) {
      config.headers.Authorization = `Bearer ${accessToken}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

/**
 * A single in-flight refresh, shared by every request that hits a 401.
 *
 * Without this, a page that fires six requests on mount gets six concurrent
 * refreshes. Each rotates the refresh token, so five of them present a token
 * that the first already invalidated, reuse detection fires, and the entire
 * session is revoked -- logging the user out for being slightly unlucky.
 */
let refreshInFlight: Promise<string | null> | null = null;

function refreshOnce(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;

  const headers: Record<string, string> = {};
  if (refreshToken) headers.Authorization = `Bearer ${refreshToken}`;

  refreshInFlight = axios
    .post(`${AUTH_URL}/api/v1/auth/refresh`, {}, {
      withCredentials: true,
      headers,
      timeout: 10000,
    })
    .then((response) => {
      adoptTokens(response.data);
      return response.data.access_token ?? null;
    })
    .catch(() => {
      forgetTokens();
      return null;
    })
    .finally(() => {
      refreshInFlight = null;
    });

  return refreshInFlight;
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;

    // Do not try to refresh a failed refresh; that recurses until the tab hangs.
    const isAuthCall =
      typeof original?.url === 'string' &&
      (original.url.includes('/auth/refresh') || original.url.includes('/auth/login'));

    if (error.response?.status === 401 && !original._retry && !isAuthCall) {
      original._retry = true;

      const token = await refreshOnce();
      if (token) return api(original);

      forgetTokens();
      // Only bounce to login if the app is mounted somewhere to show it.
      if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }

    return Promise.reject(error);
  }
);

// --- Auth API ---------------------------------------------------------------

export async function apiLogin(
  email: string,
  password: string
): Promise<AuthResponse> {
  const response = await api.post('/auth/login', { email, password });
  adoptTokens(response.data);
  return response.data;
}

export const apiRegister = async (
  email: string,
  password: string,
  displayName: string = ''
) => {
  const response = await api.post('/auth/register', {
    email,
    password,
    full_name: displayName,
  });

  if (response.status !== 200) {
    throw new Error(response.data?.detail || 'Registration failed');
  }

  adoptTokens(response.data);
  return response.data as AuthResponse;
};

export const apiMe = async (): Promise<AuthUser> => {
  if (!accessToken) throw new Error('Not authenticated');
  const response = await api.get('/users/me');
  return toAuthUser(response.data);
};

export const apiRefresh = async (): Promise<AuthResponse> => {
  const token = await refreshOnce();
  if (!token) throw new Error('No refresh token available');
  return {
    access_token: token,
    refresh_token: refreshToken,
    token_type: 'bearer',
    expires_in: 0,
  };
};

export const apiForgotPassword = async (email: string): Promise<{ message: string }> => {
  const response = await api.post('/auth/forgot-password', { email });
  return response.data;
};

export const apiResetPassword = async (
  token: string,
  new_password: string
): Promise<{ message: string }> => {
  const response = await api.post('/auth/reset-password', { token, new_password });
  return response.data;
};

export const apiOAuthAuthorize = async (
  provider: string
): Promise<{ authorization_url: string }> => {
  const response = await api.get(`/auth/oauth/${provider}/authorize`);
  return response.data;
};

// Context management
export const apiGetContexts = async () => {
  const response = await api.get('/me/contexts');
  return response.data;
};

export const apiSetLastContext = async (context: any) => {
  const response = await api.post('/me/last-context', { context });
  return response.data;
};

export const apiResolveContext = async (redirectUri?: string) => {
  const params = redirectUri ? `?redirect_uri=${encodeURIComponent(redirectUri)}` : '';
  const response = await api.get(`/me/resolve-context${params}`);
  return response.data;
};

export async function apiLogout(): Promise<void> {
  try {
    // The backend clears the cookie itself, so this must be sent with
    // credentials even when there is no in-memory token left to send.
    await api.post('/auth/logout', {}, { withCredentials: true });
  } catch {
    // A logout that fails server-side must still clear the client, otherwise
    // the user is stuck in a signed-in-looking state with no way out.
  } finally {
    forgetTokens();
  }
}

// OAuth/Social login functions
export const initiateOAuth = async (
  provider: string
): Promise<{ authorization_url: string; state: string }> => {
  const response = await api.get(`/auth/oauth/${provider}/authorize`);
  return response.data;
};

export const unlinkOAuthAccount = async (
  provider: string
): Promise<{ message: string }> => {
  const response = await api.post(`/auth/oauth/${provider}/unlink`);
  return response.data;
};

/**
 * Complete an OAuth sign-in.
 *
 * The provider callback redirects here carrying a single-use, 120-second
 * `code` and nothing else -- no token has ever travelled in a URL. The code is
 * traded over POST for the tokens it stands for. It returns the token payload,
 * not a user: callers want the user, so they call apiMe() afterwards, exactly
 * as they do after a password login.
 */
export const handleOAuthCallback = async (code: string): Promise<AuthResponse> => {
  const response = await api.post('/auth/oauth/exchange', { code }, {
    withCredentials: true,
  });
  adoptTokens(response.data);
  return response.data;
};

/** True once a credential has been adopted by this module. */
export function isAuthenticated(): boolean {
  return accessToken !== null;
}

/** Test seam: reset module state between assertions. */
export function __resetAuthStateForTests(): void {
  forgetTokens();
  delivery = 'unknown';
}

// Organization API functions
export async function apiCreateOrganization(name: string) {
  const response = await api.post('/organizations', { name });
  return response.data;
}

export async function apiGetOrganizations() {
  const response = await api.get('/organizations');
  return response.data;
}

export async function apiUpdateProfile(display_name?: string, locale?: string) {
  const response = await api.patch('/users/me', { display_name, locale });
  return response.data;
}

export { api };
export default api;