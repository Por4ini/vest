# Vestwell Dashboard (Docker Compose + PostgreSQL + Gateway)

## Запуск за 1 команду

```bash
cd /Users/sergio/vest_test
docker compose up --build
```

Після старту UI доступний тут:

`http://localhost:3006`

Трафік проходить через nginx gateway, який прокидує запити в FastAPI контейнер і тримає WebSocket.

## Що є в складі

- FastAPI панель на контейнері `app`
- PostgreSQL база:
  - історія задач (`tasks`)
  - історія проксі (`proxies`)
  - по-рядкові результати (`task_rows`)
  - події/логи (`task_events`)
- Gateway (`nginx`) на порті `3006`
- Ротаційні логи у `data/logs/dashboard.log` (на томі `app_data`)
- Результати (`json`, `csv`) зберігаються у `data/results` (на томі `app_data`)

## Корисні маршрути

- `GET /api/proxies`
- `POST /api/proxies`
- `DELETE /api/proxies/{id}`
- `POST /api/proxies/{id}/check`
- `POST /api/tasks`
- `GET /api/tasks`
- `GET /api/tasks/{id}`
- `GET /api/tasks/{id}/events`
- `GET /api/tasks/{id}/result/json`
- `GET /api/tasks/{id}/result/csv`
- `GET /api/logs`
- `GET /ws/{task_id}`

## Корисно перед локальним оновленням

```bash
docker compose down -v
docker compose up --build
```

`-v` скидає БД і томи з історією/результатами.
