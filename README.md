# Global Detergent Factory AI Sales Agent

Development runs entirely in Docker. Docker Desktop with Compose is the only host
runtime prerequisite; host Python, uv, Redis, and PDF libraries are not required.

## Development workflow

Create the ignored local environment file once if it is absent:

```powershell
Copy-Item .env.example .env
```

Keep `AI_ENABLED=false` until real integration credentials and an available model are
configured. Do not invent credentials or commit `.env`.

Start the API and Redis, then open <http://localhost:8000/docs>:

```console
docker compose up --build -d
docker compose exec api uv run --frozen pytest
docker compose exec api uv run --frozen ruff check .
docker compose exec api uv run --frozen mypy app
docker compose logs -f api
docker compose down
```

`docker compose down` keeps the `quote_storage` and `redis_data` named volumes, so
generated quotations and Redis data survive container replacement.

If the API command cannot start, run tests in a one-off API container. This uses Redis
database 15 for tests; normal development uses database 0 from `.env`.

```console
docker compose run --rm -e APP_ENV=test -e REDIS_URL=redis://redis:6379/15 api uv run --frozen pytest
```

Application health endpoints are intentionally deferred to Task 37.
