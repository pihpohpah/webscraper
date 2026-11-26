# Docker и деплой

## Файлы окружения

- `.env`: секреты и настройки клиентов приложения (`OPENAI_API_KEY`, `OPENAI_API_BASE`, при
  необходимости proxy). Compose передаёт его в контейнер через `env_file`.
- `deploy.env`: несекретные параметры образа, HTTP-порта, ресурсов и SSH-деплоя. Из него Compose
  берёт подстановки, а настройки SSH и registry для скрипта в контейнер не попадают.

Оба файла исключены из Git и из Docker build context, в репозитории лежат только
`.env.example` и `deploy.env.example`. Параметры поиска задаются в `config.yaml`.

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

Состояние и файловые логи хранятся в named volumes `event-agent-data` и `event-agent-logs`.
`docker compose down` их не трогает, а `down -v` удалит состояние дедупликации.

## Деплой на Linux-сервер

Локально нужен Docker с buildx. На сервере нужны Docker Engine с Compose plugin, SSH-доступ и
право `DEPLOY_USER` запускать Docker. Выполните `docker login ghcr.io` локально и на сервере,
токен registry в `deploy.env` не записывайте. Для ARM-сервера поменяйте `BUILD_PLATFORM` на
`linux/arm64`.

Перед первым деплоем передайте секреты отдельно (пример для значений из `deploy.env.example`):

```bash
ssh deploy@server.example.com 'install -d -m 700 /opt/event-agent'
scp .env deploy@server.example.com:/opt/event-agent/.env
ssh deploy@server.example.com 'chmod 600 /opt/event-agent/.env'
```

Затем заполните в `deploy.env` `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` и остальное и
запустите:

```bash
./scripts/deploy.sh
```

Скрипт собирает Linux-образ и публикует его в GHCR с тегом из короткого Git SHA (или из
`EVENT_AGENT_TAG`). На сервер он копирует только `compose.yaml`, `config.yaml` и несекретный
`deploy.env`, затем делает `pull` и `up -d` и ждёт статуса healthy. Секретный `.env` скрипт не
копирует и не печатает. С `SKIP_BUILD=1` скрипт выкатывает уже опубликованный образ без сборки.

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

При обновлении сервис перезапускается: на несколько секунд API недоступен, история прогонов из
памяти теряется. JSON-состояние на volume сохраняется.

## Резервная копия

Копию снимают на сервере. На это время сервис останавливают, чтобы он не писал в архивируемые
файлы:

```bash
cd /opt/event-agent
docker compose --env-file deploy.env stop event-agent
docker run --rm -v event-agent_event-agent-data:/data:ro -v "$PWD":/backup \
  alpine:3.22 tar czf /backup/event-agent-state.tar.gz -C /data .
docker compose --env-file deploy.env start event-agent
```

Префикс volume совпадает с `COMPOSE_PROJECT_NAME`. Если вы его меняли, найдите имя через
`docker volume ls`.

## Ресурсы

Большую часть прогона сервис ждёт сеть: поиск ddgs с паузами между запросами и ответы LLM. На
замере `run --dry-run` поиск шёл 900 с и израсходовал 4,7 с CPU. Пик нагрузки составил 0,7 ядра
за секунду, потоков было 16, RSS дошёл до 261 MiB. Сразу после старта процесс занимает около
200 MiB, а 24 страницы в 6 потоков скачиваются меньше чем за 1 с CPU. Полный прогон с LLM не
замеряли, поэтому в таблице оставлен запас памяти под ответы модели и историю `history_size`.

| | requests | limits |
|---|---|---|
| CPU | 100m | 1 |
| RAM | 512Mi | 1Gi |

Лимиты Compose по умолчанию (`CPU_LIMIT=2.0`, `MEMORY_LIMIT=2g`) заметно больше нужного.

Реплика должна быть одна: состояние хранится в локальном JSON под `flock`, а история прогонов
лежит в памяти процесса. При обновлении старый экземпляр нужно остановить до запуска нового, в
Kubernetes для этого есть стратегия `Recreate`. Сервис пишет в `/app/data` (нужен постоянный
том, хватит 100 MiB), в `/app/logs` (там же лежит lock-файл) и в `/tmp`. На остановку уходит до
60 с: сервис сворачивает текущий прогон и сохраняет состояние.

## Сетевой доступ

По умолчанию `HTTP_BIND=0.0.0.0` публикует порт на всех интерфейсах сервера. У API нет ни
авторизации, ни TLS, поэтому закройте порт firewall или security group. Если нужен доступ из
интернета, поставьте перед сервисом reverse proxy с TLS и контролем доступа, а `HTTP_BIND`
переключите на `127.0.0.1`.

Исходящий трафик идёт на TCP 443, к сайтам на http ещё на 80, и нужен DNS:

- LLM: хост из `OPENAI_API_BASE`, то есть `api.kimi.ai` или `api.kimi.com`.
- Поисковики ddgs. Библиотека выбирает поисковик случайно, поэтому открыть нужно все:
  `www.bing.com`, `html.duckduckgo.com`, `duckduckgo.com`, `search.brave.com`, `www.google.com`,
  `www.mojeek.com`, `www.startpage.com`, `search.yahoo.com`, `news.search.yahoo.com`,
  `ru.wikipedia.org`, `grokipedia.com`.
- Страницы из выдачи. Домены там любые, белым списком их не описать, поэтому нужен выход в
  интернет на 80/443 или HTTP-proxy. Переменные `HTTPS_PROXY`/`HTTP_PROXY` из `.env` учитывают
  и ddgs, и litellm.
- `raw.githubusercontent.com`. Оттуда litellm при старте скачивает таблицу цен моделей. Если
  доступа нет, он берёт встроенную копию, а `LITELLM_LOCAL_MODEL_COST_MAP=True` отключает
  запрос совсем.

Чтобы скачать образ, узлу нужен `ghcr.io`, слои отдаются с `pkg-containers.githubusercontent.com`.
