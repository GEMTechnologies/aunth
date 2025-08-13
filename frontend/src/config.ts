
export const AUTH_URL: string = (window as any).__AUTH_URL__ || process.env.AUTH_URL || "http://0.0.0.0:8000";

export const API_CONFIG = {
  baseURL: AUTH_URL,
  timeout: 10000,
};
