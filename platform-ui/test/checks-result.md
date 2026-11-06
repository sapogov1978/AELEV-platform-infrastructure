# Platform UI — проверки поставки

Дата: 2026-11-01T23:18:51.022585+00:00. Итог: PASS. Длительность: 28.5 с.

Python-проверки выполнены на Python 3.13 в собственном образе без Docker socket, сервер API использует те же закреплённые версии. Frontend проверен собственными Vitest/TypeScript/Vite.

## PASS: `npm test`

```text
> @aelev/platform-ui@0.1.0 test
> vitest run


 RUN  v4.1.12 <source-repository>/services/infrastructure/platform-ui


 Test Files  4 passed (4)
      Tests  105 passed (105)
   Start at  00:18:25
   Duration  437ms (transform 437ms, setup 0ms, import 614ms, tests 146ms, environment 0ms)
```

## PASS: `npm run build`

```text
> @aelev/platform-ui@0.1.0 build
> tsc -b && vite build

vite v7.3.7 building client environment for production...
transforming...
✓ 35 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.53 kB │ gzip:  0.33 kB
dist/assets/index-b7LoOYl3.css   11.06 kB │ gzip:  3.00 kB
dist/assets/index-BT8_aXX6.js   228.52 kB │ gzip: 72.11 kB
✓ built in 607ms
```

## PASS: `docker compose --env-file .env config --quiet`

```text

```

## PASS: `docker run --rm aelev/platform-ui-checks:local black --check api.py prepare.py provision.py health.py publisher_authority.py test`

```text
All done! ✨ 🍰 ✨
22 files would be left unchanged.
```

## PASS: `docker run --rm aelev/platform-ui-checks:local ruff check api.py prepare.py provision.py health.py publisher_authority.py test`

```text
All checks passed!
```

## PASS: `docker run --rm aelev/platform-ui-checks:local mypy api.py prepare.py provision.py health.py publisher_authority.py`

```text
Success: no issues found in 5 source files
```

## PASS: `docker run --rm aelev/platform-ui-checks:local pytest -q`

```text
........................................................................ [ 30%]
........................................................................ [ 60%]
........................................................................ [ 90%]
......................                                                   [100%]
=============================== warnings summary ===============================
../opt/venv/lib/python3.13/site-packages/fastapi/testclient.py:1
  /opt/venv/lib/python3.13/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

../opt/venv/lib/python3.13/site-packages/starlette/testclient.py:53
  /opt/venv/lib/python3.13/site-packages/starlette/testclient.py:53: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
238 passed, 2 warnings in 3.78s
```

Проверка конфигурации не является запуском общей оркестрации. Настоящий вход, TLS/mTLS, изоляция и очистка проверяются отдельно в [жизненном цикле](lifecycle-result.md).
