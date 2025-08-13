import { AUTH_URL } from "../config";

export async function apiLogin(email: string, password: string) {
  const res = await fetch(`${AUTH_URL}/auth/login`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({email, password})
  });
  if (!res.ok) throw new Error((await res.json()).detail || "Login failed");
  return res.json() as Promise<{ access_token: string; refresh_token: string; token_type: string }>;
}

export async function apiRegister(email: string, password: string, full_name?: string) {
  const res = await fetch(`${AUTH_URL}/auth/register`, {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({email, password, full_name})
  });
  if (!res.ok) throw new Error((await res.json()).detail || "Registration failed");
  return res.json();
}

export async function apiMe(access: string) {
  const res = await fetch(`${AUTH_URL}/users/me`, {
    headers: { Authorization: `Bearer ${access}` }
  });
  if (!res.ok) throw new Error("Unauthorized");
  return res.json();
}

export async function apiRefresh(refresh: string) {
  const res = await fetch(`${AUTH_URL}/auth/refresh`, {
    method: "POST",
    headers: { Authorization: `Bearer ${refresh}` }
  });
  if (!res.ok) throw new Error("Refresh failed");
  return res.json() as Promise<{ access_token: string; refresh_token: string; token_type: string }>;
}
