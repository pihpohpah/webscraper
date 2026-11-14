# event-agent

HTTP-сервис поиска анонсов событий. Другой сервис дёргает ручку `POST /runs`: агент ищет анонсы
в интернете (ddgs), извлекает события через LLM (Kimi Code) и отдаёт структурированный JSON.
Базы данных нет — между прогонами хранится маленький JSON-файл, чтобы не отдавать одни и те же
события дважды.

## Быстрый старт

```bash
cd event-agent
uv sync
cp .env.example .env                                  # вписать OPENAI_API_KEY и OPENAI_API_BASE
uv run --env-file .env event-agent serve --port 8000  # терминал 1: сервис

curl -s -X POST localhost:8000/runs                   # терминал 2: запустить прогон → run_id
curl -s localhost:8000/runs/<run_id>                  # опрашивать, пока status == running
```

Прогон занимает 3–15 минут (поиск и LLM), поэтому ответ асинхронный.

## API

| Метод | Путь | Что делает |
|---|---|---|
| `POST` | `/runs` | Запускает полный прогон, `202` + `run_id`. Если прогон уже идёт — возвращает его (`already_running: true`). `409`, если состояние занято другим процессом (например, CLI-прогоном). |
| `GET` | `/runs/{run_id}` | `status`: `running` → `completed` / `partial` (сработал `run_timeout`, отдано собранное) / `failed` (см. `error`). После завершения — `events` и `stats`. `404` для неизвестного id. |
| `GET` | `/health` | Жив ли сервис и `active_run_id`. |

Интерактивная документация OpenAPI — `/docs`. Авторизации нет: сервис рассчитан на внутреннюю сеть.

Пример ответа `GET /runs/{run_id}` (сокращён):

```json
{
  "run_id": "a42ca2a68473",
  "status": "completed",
  "already_running": false,
  "started_at": "2026-11-13T21:39:24+10:00",
  "finished_at": "2026-11-13T21:45:27+10:00",
  "timed_out": false,
  "error": null,
  "stats": {"found": 70, "skipped": 0, "quarantined": 0, "deferred": 58, "read": 12,
            "failed": 0, "new": 124, "updated": 0, "seconds": 266.0,
            "search_seconds": 265.9, "tail_seconds": 0.1,
            "fetch_busy_seconds": 23.1, "llm_busy_seconds": 447.2},
  "events": [
    {
      "id": "442b6e21be4ff1a9216bf9e4255bca423ef72c7f",
      "change": "new",
      "title": "Кот в сапогах",
      "category": "kids",
      "start_date": "2026-11-14",
      "end_date": null,
      "start_time": "12:00",
      "city": "Владивосток",
      "venue": "Приморская краевая филармония",
      "price": "от 300 ₽",
      "is_free": false,
      "description": "Детский спектакль «Кот в сапогах»…",
      "event_url": null,
      "is_cancelled": false,
      "source_urls": ["https://vladivostok.ticketland.ru/spectacle"]
    }
  ]
}
```

**Поля события.** `category` — одно из `concert`, `festival`, `exhibition`, `theater`, `lecture`,
`workshop`, `meetup`, `conference`, `standup`, `tour`, `quiz`, `kids`, `film`, `other`.
`start_time` — `HH:MM` или `null`; `is_free` — `true`/`false`/`null` (неизвестно);
`event_url` — только `http(s)`, для карточек подставляется адрес страницы.

**Что попадает в `events`.** Только события, которых ещё не отдавали (`change: "new"`), и
существенно изменившиеся (`change: "updated"` — появилась площадка или время начала, событие
отменено). Ключ `id` стабилен между прогонами. Новые идут первыми, затем по дате и времени.

**Счётчики `stats`.** `found` — уникальных URL после фильтров, `skipped` — уже прочитанные,
`quarantined` — в карантине, `deferred` — не вошли в лимит `pages_per_run` (уйдут в следующий
прогон), `read` / `failed` — страницы, обработанные LLM / не прочитанные. Замеры:
`search_seconds` — время поиска, `tail_seconds` — сколько страницы дожимались после поиска
(чем меньше, тем лучше они перекрылись с поиском), `fetch_busy_seconds` / `llm_busy_seconds` —
суммарная работа всех потоков (делите на `workers`, чтобы оценить загрузку).

**Что хранится.**
- Результаты последних `history_size` прогонов лежат **в памяти процесса** и пропадают при
  перезапуске (`GET` вернёт `404`).
- Состояние дедупликации (`state_path`) лежит на диске и перезапуск переживает.
- Поэтому забирайте результат после завершения прогона: события уже помечены как отданные, и в
  следующем прогоне они не повторятся.

**Один процесс.** Запускать нужно один процесс (`serve` без `--workers`): активный прогон хранится
в памяти. Второй процесс или CLI-прогон блокируется через `flock` на `lock_path`, ручка вернёт `409`.

## Установка и LLM

```bash
uv sync
cp .env.example .env
```

### Подключение Kimi напрямую (подписка Kimi Code)

1. Создать API key в [консоли Kimi Code](https://www.kimi.com/code/console) — показывается один раз.
2. `.env` — ключ и хост, соответствующий аккаунту:

```bash
OPENAI_API_KEY=sk-kimi-...
OPENAI_API_BASE=https://api.kimi.ai/coding/v1    # зарубежный аккаунт
# OPENAI_API_BASE=https://api.kimi.com/coding/v1  # китайский аккаунт
```

3. `config.yaml` — модель: `kimi-for-coding` (K2.8 Preview), `kimi-for-coding-highspeed`
   (K2.7 Code Highspeed), `k3`, `k3-256k`, `k3.1-pro`.

Важно: у Kimi Code путь **`/coding/v1`**, а не `/v1`. Переменные называются `OPENAI_*`, потому
что это имя стандарта, который читает litellm, — сам сервер при этом Kimi. Ключ от консоли
Kimi Code не подходит к Open Platform (`api.moonshot.ai`) и наоборот.

Особенности Kimi Code, из-за которых код написан именно так:

- **`temperature` не передаётся.** Модели с рассуждением принимают только `temperature=1`; на `0`
  API отвечает `400 invalid temperature: only 1 is allowed for this model`. Для обычных моделей,
  которым нужен `temperature: 0`, добавьте в `config.yaml` строку `temperature: 0`.
- **`reasoning_effort: low`** — по умолчанию K2.8 Preview думает на `max`, что даёт лишние
  секунды и расход квоты на извлечение JSON. Допустимые значения: `low`/`high`/`max`.
- Модель иногда возвращает невалидный JSON с неэкранированными кавычками
  (`"…рождение футуризма и "Черный квадрат"…"`). Такие ответы чинятся автоматически.
- Временные сбои LLM (таймаут, сеть, rate limit, 5xx) повторяются `llm_retries` раз; остальные
  ошибки (например, 400) — нет, страница уходит в неудачные.

## Запуск

```bash
uv run --env-file .env event-agent                  # HTTP-сервис (то же, что `serve`)
uv run --env-file .env event-agent serve --port 8000 [--host 0.0.0.0]
uv run --env-file .env event-agent run              # один прогон из консоли, JSON в stdout
uv run --env-file .env event-agent run --dry-run    # только список URL, без LLM и без записи
uv run --env-file .env event-agent run --general    # принудительно включить общий поиск
uv run --env-file .env event-agent --config /path/config.yaml run
```

`--config` указывается **до** подкоманды. Логи идут в консоль (stderr) и в `logs/event-agent.log`;
в конце каждого прогона — одна итоговая строка со счётчиками. Cron не нужен: прогон запускает
вызывающий сервис.

## Проверка наполнения данных

```bash
# Разовый прогон без HTTP: события в файл, логи на экране
uv run --env-file .env event-agent run > out.json

# Сводка по результатам
python3 -c "
import json
for e in json.load(open('out.json')):
    print(e['start_date'], e['start_time'] or '--:--', e['change'], '|', e['venue'] or '—', '|', e['title'])
"

# Что запомнило состояние
python3 -c "
import json, collections
s = json.load(open('data/state.json'))
print('events:', len(s['events']))
print('pages:', collections.Counter(p['status'] for p in s['pages'].values()))
"
```

- Повторный прогон должен вернуть только новое и изменившееся.
- Чтобы начать с чистого листа, удалите `data/state.json`.
- Для быстрой проверки скопируйте `config.yaml`, поставьте `pages_per_run: 3` и `general: false`,
  затем запускайте `event-agent --config копия.yaml run`.
- Страницы со статусом `fetch_failed` / `llm_failed` и счётчиком `fails` в `state.json` — это те,
  что будут повторены (или уже в карантине, если `until` в будущем).

## Настройка

`config.yaml` (относительные пути считаются от каталога конфига):

| Параметр | Значение |
|---|---|
| `model` | модель для litellm, например `openai/kimi-for-coding` (обязателен) |
| `reasoning_effort` | `low`/`high`/`max` для Kimi; убрать, если модель не поддерживает |
| `temperature` | необязательный; для Kimi Code не указывать (принимает только 1) |
| `llm_timeout`, `llm_timeout_listing`, `llm_retries` | таймаут вызова модели для карточки и для афиши (до 40 событий), с, и число повторов при временных сбоях (90, 240, 1) |
| `timezone` | часовой пояс «сегодня» (`Asia/Vladivostok`) |
| `region` | регион ddgs (`ru-ru`) |
| `cities` | города для `{city}` в `general_queries` |
| `resources` | домены с `queries` и (необязательно) `watch_urls`, `url_contains` |
| `resources[].url_contains` | подстроки URL для отсева чужого города, например `/vladivostok/` |
| `general_queries` | шаблоны общего поиска: `news` (за сутки) и `text` (за неделю) |
| `general` | включать ли общий поиск в каждый прогон (`true`) |
| `blocked_domains` | домены и поддомены, которые не читаем (в том числе JS-сайты `afisha.ru`, `timepad.ru`: они отдают пустой текст) |
| `pages_per_run` | максимум страниц в LLM за прогон (25) |
| `page_chars` | обрезка текста страницы перед LLM (12000) |
| `max_events_listing`, `max_events_article` | максимум событий с афиши / с карточки (40 / 10) |
| `workers` | сколько страниц качать и отправлять в LLM одновременно (6); растите осторожно — упрётесь в лимиты Kimi |
| `search_workers` | сколько поисковых запросов идти параллельно (3) |
| `run_timeout` | жёсткий лимит прогона, с; по истечении — `partial` (900) |
| `state_path` | файл состояния (`data/state.json`) |
| `state_ttl_days` | через сколько дней забывать прочитанные статьи (30) |
| `quarantine_after`, `quarantine_days` | после скольких неудач подряд статья уходит в карантин и на сколько дней (3, 7) |
| `history_size` | сколько последних прогонов держать в памяти (20) |
| `server.host`, `server.port` | адрес сервиса (`127.0.0.1:8000`) |
| `log_path`, `lock_path` | пути логов и lock-файла |

Порядок обработки за прогон: `watch_urls` → поиск по ресурсам (`"<запрос> site:<domain>"`) →
общий поиск (если `general: true`). При нехватке `pages_per_run` афиши имеют приоритет над статьями.

**Общий поиск** (`general_queries`) — это `news` за сутки и `text` за неделю, с подстановкой
`{city}`. Сейчас 4 + 29 фраз, покрывающих все категории событий: концерты и филармония,
спектакли и драмтеатр, выставки (музей, галерея), лекции, мастер-классы, тренинги, митапы,
конференции, стендап, квизы, экскурсии, кинопоказы, детские мероприятия, а также типовые
запросы «куда сходить», «куда пойти», «досуг», «отдых на выходные», «бесплатные мероприятия».

Источники-афиши, где площадка указана в тексте рядом с событием: `afishagorodov.ru`,
`vladivostok.ticketland.ru`, `vl.kassir.ru`. Они дают `venue` почти для всех событий.
`primcult.ru` проверялся, но площадок в тексте не содержит — не добавлен. `vl.ru` не используется.

Формулировки подобраны экспериментально: ddgs чувствителен к wording. Например,
`выставка Владивосток` даёт 0 результатов, а `выставка Владивосток 2026` и
`выставка Владивосток музей` — по 7–8. Пустая выдача на части запросов — норма, не ошибка.

## Как устроено

```
POST /runs → Runner (один активный прогон, фоновый поток, flock)
  → state.prune        убрать завершённые события и старые статьи
  → афиши (watch_urls) сразу уходят в пул: скачать → сравнить хэш → LLM (если изменилась)
  → поиск (ddgs)       запросы идут параллельно; каждая найденная статья сразу ставится в тот
                       же пул (скачать → LLM), не дожидаясь остальных запросов
  → сбор результатов   основной поток применяет итоги к состоянию: дедупликация, new / updated
  → сохранение state.json (атомарно, в том числе при partial)
```

Фазы не ждут друг друга: пока идёт поиск, афиши и ранее найденные статьи уже обрабатываются
моделью. Рабочие потоки состояние только читают, меняет его один основной поток. Лимит
`pages_per_run` общий: афиши резервируют места заранее, статьи берут остаток.

- **Страницы в состоянии.** `article` (из поиска) читается один раз. `listing` (`watch_urls`)
  грузится каждый прогон; при том же хэше (первые `page_chars` символов, пробелы не в счёт)
  LLM не зовётся.
- **Неудачи.** Страницы со статусом `llm_failed` и `fetch_failed` повторяются в следующих
  прогонах; после `quarantine_after` неудач подряд статья уходит в карантин на `quarantine_days`
  дней; статья с пустым текстом (JS-сайт) уходит в карантин сразу, без повторов. Афиши в
  карантин не попадают и пробуются каждый прогон. `410 Gone` считается
  окончательным (статус `gone`). Хэш афиши при сбое не сохраняется, поэтому она будет
  перечитана.
- **События.** Ключ — `SHA-1(название|дата|город)` плюс нечёткое сопоставление за ту же дату
  (`difflib` ≥ 0.85 или вхождение названия не короче 8 символов и не меньше половины длины
  более длинного). Разные город, площадка или время начала — разные события (два сеанса
  спектакля в один день не склеиваются).
- **Очистка.** В начале прогона удаляются события после даты окончания и статьи старше TTL.
- **Дедлайн.** Паузы, повторы и вызовы LLM учитывают остаток времени `run_timeout`. По
  истечении прогон завершается со статусом `partial` и отдаёт собранное. При остановке сервиса
  (SIGINT/SIGTERM) активный прогон сворачивается и сохраняет состояние.

Структура кода (`src/event_agent/`): `config.py`, `deadline.py`, `search.py`, `llm.py`,
`state.py`, `pipeline.py`, `runner.py`, `schemas.py`, `api.py`, `main.py`.

## Docker и деплой

### Файлы окружения

- `.env` — секреты и настройки клиентов приложения (`OPENAI_API_KEY`, `OPENAI_API_BASE`, при
  необходимости proxy). Compose передаёт этот файл в контейнер через `env_file`.
- `deploy.env` — не секретные параметры образа, HTTP-порта, ресурсов и SSH-деплоя. Эти значения
  используются для подстановок Compose; SSH- и registry-настройки скрипта в контейнер не передаются.

Оба файла игнорируются Git и Docker build context. В репозитории хранятся только `.env.example` и
`deploy.env.example`. Рабочие параметры поиска остаются в `config.yaml`.

```bash
cp .env.example .env                    # заполнить ключ
cp deploy.env.example deploy.env        # заполнить параметры площадки
chmod 600 .env
```

### Локальный запуск

```bash
docker build -t event-agent:local .
EVENT_AGENT_IMAGE=event-agent EVENT_AGENT_TAG=local PULL_POLICY=never \
  docker compose --env-file deploy.env up -d
curl --fail http://127.0.0.1:8000/health
docker compose --env-file deploy.env logs -f event-agent
```

Состояние и файловые логи сохраняются в named volumes `event-agent-data` и `event-agent-logs`.
`docker compose down` их сохраняет; `down -v` удалит состояние дедупликации.

### Деплой на Linux-сервер

Нужны Docker с buildx локально, Docker Engine и Compose plugin на сервере, SSH-доступ и право
пользователя `DEPLOY_USER` запускать Docker. Локально и на сервере выполнить `docker login ghcr.io`.
Токен registry в `deploy.env` не записывать. Для ARM-сервера изменить `BUILD_PLATFORM` на
`linux/arm64`.

Перед первым деплоем отдельно передать секреты (пример для значений из `deploy.env.example`):

```bash
ssh deploy@server.example.com 'install -d -m 700 /opt/event-agent'
scp .env deploy@server.example.com:/opt/event-agent/.env
ssh deploy@server.example.com 'chmod 600 /opt/event-agent/.env'
```

Затем заполнить `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH` и другие параметры в `deploy.env`:

```bash
./scripts/deploy.sh
```

Скрипт собирает Linux-образ, публикует его в GHCR с тегом короткого Git SHA (или
`EVENT_AGENT_TAG`), копирует на сервер только Compose, `config.yaml` и не секретный `deploy.env`,
выполняет `pull` и `up -d`, затем ждёт healthy-статуса. Секретный `.env` скрипт не копирует и не
выводит. Для повторного выката уже опубликованного образа без сборки используется `SKIP_BUILD=1`.

Частые операции на сервере:

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

Резервная копия состояния делается на сервере; остановка исключает запись во время архивации:

```bash
cd /opt/event-agent
docker compose --env-file deploy.env stop event-agent
docker run --rm -v event-agent_event-agent-data:/data:ro -v "$PWD":/backup \
  alpine:3.22 tar czf /backup/event-agent-state.tar.gz -C /data .
docker compose --env-file deploy.env start event-agent
```

Если изменён `COMPOSE_PROJECT_NAME`, префикс volume в команде backup будет другим; его можно
посмотреть через `docker volume ls`.

**Сетевой доступ.** По умолчанию `HTTP_BIND=0.0.0.0` публикует порт на всех интерфейсах сервера.
У API нет авторизации и TLS, поэтому порт нужно ограничить firewall/security group. Для доступа из
интернета следует использовать reverse proxy с TLS и контролем доступа, а `HTTP_BIND` установить в
`127.0.0.1`.

Обновление не является zero-downtime: запускается только один экземпляр, во время перезапуска API
кратко недоступен, а история прогонов в памяти теряется. JSON-состояние на volume сохраняется.

## Разработка

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run pytest -q
```

Тесты не ходят в сеть и не вызывают LLM: поиск, загрузка и модель подменяются.

## Ограничения

- Сайты, которые рисуют афишу на JavaScript (timepad.ru, afisha.ru), отдают пустой текст —
  такие статьи получают `fetch_failed` и сразу уходят в карантин; `afisha.ru` и `timepad.ru`
  добавлены в `blocked_domains`.
- ddgs — метапоиск: он упирается в лимиты поисковиков (`RatelimitException`) и иногда возвращает
  нерелевантную выдачу. Один повтор через 30 секунд, затем запрос пропускается.
- Модель извлекает события только из текста страницы: если даты нет в тексте (например, афиша в
  картинках), событие не найдётся.
- `venue` заполняется только там, где площадка есть на странице. Страницы-афиши списком адресов
  не содержат — их приходится добирать с карточек событий (`article`), поэтому у событий с
  `listing` поле обычно пустое.
- `pages_per_run` ограничивает число страниц, которые за прогон уходят в модель; остальные
  попадут в следующий прогон.
- Поиск по `site:<домен>` без пути отдаёт выдачу по всем регионам: запрос
  «концерт Владивосток site:gorodzovet.ru» приносил события Мурманска и Казани, и бюджет
  страниц сгорал на них. Поэтому у ресурса задаётся `url_contains: [/vladivostok/]`.
- Дубли с разными `start_date` не склеиваются (сравнение идёт в пределах одной даты).
- Результаты прогонов не переживают рестарт процесса (см. раздел API).
