# Granada Auth Service (Drop-in Backend)

This backend provides registration, login, JWT access/refresh, logout, organizations, and a `/users/me` endpoint.

## Quickstart (SQLite)

```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env  # then edit if needed
uvicorn backend.main:app --reload
```

Visit: `http://localhost:8000/docs`

## PostgreSQL (recommended)
Set `DATABASE_URL` in `backend/.env`, e.g.
```
postgresql+psycopg2://granada:granada@localhost:5432/granada_auth
```

## Endpoints
- `POST /auth/register` `{email, password, full_name?}` → User
- `POST /auth/login` `{email, password}` → `{access_token, refresh_token}`
- `POST /auth/refresh` (Authorization: Bearer <refresh_token>) → rotate tokens
- `POST /auth/logout` (Authorization: Bearer <refresh_token>) → revoke
- `GET /users/me` (Authorization: Bearer <access_token>)
- `POST /orgs` (Authorization: Bearer <access_token>) `{name}` → Organization

## CORS
For local dev CORS is set to `*`. Lock this down in production.

## Frontend integration (example fetch)
```ts
// login.ts
export async function login(email: string, password: string) {
  const res = await fetch(import.meta.env.VITE_AUTH_URL + "/auth/login", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({email, password})
  });
  if (!res.ok) throw new Error("Login failed");
  return await res.json(); // {access_token, refresh_token, token_type}
}
```

## Notes
- Email verification & password reset can reuse the same JWT mechanism with `scope` values like `verify` and `reset`. Hook into your mailer to send links: `${APP_URL}/verify?token=...`
- Alembic migrations can be added if you prefer managed schema evolution.
