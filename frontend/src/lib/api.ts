import axios, { AxiosInstance } from 'axios';
import { AUTH_URL } from '../config';

// Define AuthResponse type for clarity
interface AuthResponse {
  access_token: string;
  refresh_token: string;
  user: any; // Define a more specific user type if possible
}

// Create axios instance
const api: AxiosInstance = axios.create({
  baseURL: `${AUTH_URL}/api/v1`,
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor to add auth token
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('access_token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor to handle token refresh
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;

    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;

      const refreshToken = localStorage.getItem('refresh_token');
      if (refreshToken) {
        try {
          const response = await axios.post(`${AUTH_URL}/api/v1/auth/refresh`, {}, {
            headers: { Authorization: `Bearer ${refreshToken}` }
          });

          const { access_token, refresh_token } = response.data;
          localStorage.setItem('access_token', access_token);
          localStorage.setItem('refresh_token', refresh_token);

          return api(original);
        } catch (refreshError) {
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          window.location.href = '/login';
          return Promise.reject(refreshError);
        }
      }
    }

    return Promise.reject(error);
  }
);

// Auth API functions
export async function apiLogin(email: string, password: string): Promise<AuthResponse> {
  const response = await api.post('/auth/login', { email, password });
  return response.data;
}

export const apiRegister = async (email: string, password: string, displayName: string = '') => {
  const response = await fetch(`${API_BASE_URL}/auth/register`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      email,
      password,
      display_name: displayName,
    }),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Registration failed');
  }

  return response.json();
};

export const apiMe = async () => {
  const token = localStorage.getItem('access_token');
  if (!token) throw new Error('No access token');

  const response = await api.get('/me', {
    headers: { Authorization: `Bearer ${token}` }
  });
  return response.data;
};

export async function apiRefresh(refresh_token: string): Promise<AuthResponse> {
  const response = await axios.post(`${AUTH_URL}/api/v1/auth/refresh`, {}, {
    headers: { Authorization: `Bearer ${refresh_token}` }
  });
  return response.data;
}

export async function apiLogout() {
  const refreshToken = localStorage.getItem('refresh_token');
  if (refreshToken) {
    await api.post('/auth/logout', {}, {
      headers: { Authorization: `Bearer ${refreshToken}` }
    });
  }
  localStorage.removeItem('access_token');
  localStorage.removeItem('refresh_token');
}

// OAuth/Social login functions
export const initiateOAuth = async (provider: string): Promise<{authorization_url: string, state: string}> => {
  const response = await api.get(`/auth/oauth/${provider}/authorize`);
  return response.data;
};

export const unlinkOAuthAccount = async (provider: string): Promise<{message: string}> => {
  const response = await api.post(`/auth/oauth/${provider}/unlink`);
  return response.data;
};

// Handle OAuth callback
export const handleOAuthCallback = async (provider: string, code: string, state: string): Promise<AuthResponse> => {
  const response = await api.post(`/auth/oauth/${provider}/callback`, { code, state });
  return response.data;
};

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

export default api;