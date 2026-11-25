# Docker и деплой

## Файлы окружения

- `.env` — секреты и настройки клиентов приложения (`OPENAI_API_KEY`, `OPENAI_API_BASE`, при
  необходимости proxy). Compose передаёт этот файл в контейнер через `env_file`.
- `deploy.env` — несекретные параметры образа, HTTP-порта, ресурсов и SSH-деплоя. Compose берёт
  из него подстановки; SSH- и registry-настройки скрипта в контейнер не попадают.

Оба файла игнорируются Git и Docker build context, в репозитории лежат только `.env.example` и
`deploy.env.example`. Параметры поиска остаются в `config.yaml`.

```bash
cp .env.example .env                    # заполнить ключ
cp deploy.env.example deploy.env        # заполнить параметры площадки
chmod 600 .env
```

## Локальный запуск

```bash
docker build -t event-agent:local .
EVENT_AGENT_IMAGE=event-agent EVENT_AGENT_TAG=local PULL_POLICY=never \
  docker compose --env-file deploy.env up -d
curl --fail http://127.0.0.1:8000/health
docker compose --env-file deploy.env logs -f event-agent
```

Состояние и файловые логи лежат в named volumes `event-agent-data` и `event-agent-logs`.
`docker compose down` их сохраняет, `down -v` удалит состояние дедупликации.

## Деплой на Linux-сервер

Локально нужен Docker с buildx, на сервере — Docker Engine и Compose plugin, SSH-доступ и право
`DEPLOY_USER` запускать Docker. Выполните `docker login ghcr.io` локально и на сервере; токен
registry в `deploy.env` не пишите. Для ARM-сервера поменяйте `BUILD_PLATFORM` на `linux/arm64`.

Перед первым деплоем передайте секреты отдельно (пример для значений из `deploy.env.example`):

```bash
ssh deploy@server.example.com 'install -d -m 700 /opt/event-agent'
scp .env deploy@server.example.com:/opt/event-agent/.env
ssh deploy@server.example.com 'chmod 600 /opt/event-agent/.env'
```

Затем заполните `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` и остальное в `deploy.env` и
запустите:

```bash
./scripts/deploy.sh
```

Скрипт собирает Linux-образ и публикует его в GHCR с тегом короткого Git SHA (или
`EVENT_AGENT_TAG`). На сервер он копирует только `compose.yaml`, `config.yaml` и несекретный
`deploy.env`, делает `pull` и `up -d` и ждёт статуса healthy. Секретный `.env` скрипт не копирует
и не выводит. `SKIP_BUILD=1` выкатывает уже опубликованный образ без сборки.

Частые операции:

```bash
cd /opt/event-agent
docker compose --env-file deploy.env ps
docker compose --env-file deploy.env logs --tail=100 -f event-agent
curl --fail http://127.0.0.1:8000/health

# Обновить config.yaml и Compose без пересборки того же тега:
SKIP_BUILD=1 ./scripts/deploy.sh  # запускать локально, не на сервере

# Откат на ранее опубликованный образ (запускать локально):
EVENT_AGENT_TAG=<previous_git_sha> SKIP_BUILD=1 ./scripts/deploy.sh
```

При обновлении сервис перезапускается: API несколько секунд недоступен, история прогонов в
памяти пропадает. JSON-состояние на volume остаётся.

## Резервная копия

Копия снимается на сервере. Сервис на это время останавливается, чтобы не писать в архивируемые
файлы:

```bash
cd /opt/event-agent
docker compose --env-file deploy.env stop event-agent
docker run --rm -v event-agent_event-agent-data:/data:ro -v "$PWD":/backup \
  alpine:3.22 tar czf /backup/event-agent-state.tar.gz -C /data .
docker compose --env-file deploy.env start event-agent
```

Префикс volume совпадает с `COMPOSE_PROJECT_NAME`; если вы его меняли, посмотрите имя через
`docker volume ls`.

## Сетевой доступ

По умолчанию `HTTP_BIND=0.0.0.0` публикует порт на всех интерфейсах сервера. У API нет ни
авторизации, ни TLS, поэтому закройте порт firewall или security group. Для доступа из интернета
поставьте reverse proxy с TLS и контролем доступа, а `HTTP_BIND` переключите на `127.0.0.1`.
