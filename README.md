# Granada Authentication Service

This is the central, independent service responsible for managing all user identity,
organization profiles, authentication, and authorization for the entire Granada platform.

- **Backend:** FastAPI, SQLAlchemy
- **Frontend:** React (Micro-Frontend)

## Operating it

See [docs/OPERATING.md](docs/OPERATING.md) — what the system does, what is proven, how to check its
health, and the three things left that only an operator can do.

Quick health check:

```bash
docker compose exec -T worker python /app/backend/tools/system_check.py \
  --api-base http://api:8000 --host-header granadac.com
```
