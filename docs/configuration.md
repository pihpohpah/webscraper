# Настройка

Сервису нужны два файла: `.env` с ключом LLM (шаблон `.env.example`) и `config.yaml` с
параметрами поиска. Относительные пути в `config.yaml` считаются от каталога конфига.

## Подключение Kimi Code

1. Создать API key в [консоли Kimi Code](https://www.kimi.com/code/console). Ключ показывается
   один раз.
2. Вписать в `.env` ключ и хост, соответствующий аккаунту:

   ```bash
   OPENAI_API_KEY=sk-kimi-...
   OPENAI_API_BASE=https://api.kimi.ai/coding/v1    # зарубежный аккаунт
   # OPENAI_API_BASE=https://api.kimi.com/coding/v1  # китайский аккаунт
   ```

3. Выбрать модель в `config.yaml`: `kimi-for-coding` (K2.8 Preview),
   `kimi-for-coding-highspeed` (K2.7 Code Highspeed), `k3`, `k3-256k`, `k3.1-pro`.

У Kimi Code путь `/coding/v1`, а не `/v1`. litellm читает переменные `OPENAI_*` для любого
OpenAI-совместимого сервера, поэтому они так называются, хотя сервер — Kimi. Ключ от консоли
Kimi Code не подходит к Open Platform (`api.moonshot.ai`), и наоборот.

### Особенности Kimi Code

- `temperature` не передаётся. Модели с рассуждением принимают только `temperature=1`, на `0`
  API отвечает `400 invalid temperature: only 1 is allowed for this model`. Для моделей, которым
  нужен `temperature: 0`, добавьте эту строку в `config.yaml`.
- `reasoning_effort: low`. По умолчанию K2.8 Preview думает на `max`, а для извлечения JSON это
  лишние секунды и квота. Допустимые значения: `low`/`high`/`max`.
- Модель иногда возвращает невалидный JSON с неэкранированными кавычками
  (`"…рождение футуризма и "Черный квадрат"…"`). `llm.py` чинит такие ответы автоматически.
- Временные сбои LLM (таймаут, сеть, rate limit, 5xx) повторяются `llm_retries` раз. Остальные
  ошибки (например, 400) не повторяются, страница уходит в неудачные.

## Параметры `config.yaml`

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
| `blocked_domains` | домены и поддомены, которые не читаем |
| `pages_per_run` | максимум страниц в LLM за прогон (25) |
| `page_chars` | обрезка текста страницы перед LLM (12000) |
| `max_events_listing`, `max_events_article` | максимум событий с афиши / с карточки (40 / 10) |
| `workers` | сколько страниц качать и отправлять в LLM одновременно (6); при росте упрётесь в лимиты Kimi |
| `search_workers` | сколько поисковых запросов идёт параллельно (3); выше — риск лимитов ddgs |
| `run_timeout` | жёсткий лимит прогона, с; по истечении — `partial` (900) |
| `state_path` | файл состояния (`data/state.json`) |
| `state_ttl_days` | через сколько дней забывать прочитанные страницы (30) |
| `quarantine_after`, `quarantine_days` | после скольких неудач подряд страница уходит в карантин и на сколько дней (3, 7) |
| `history_size` | сколько последних прогонов держать в памяти (20) |
| `server.host`, `server.port` | адрес сервиса (`127.0.0.1:8000`) |
| `log_path`, `lock_path` | пути логов и lock-файла |

## Поиск и источники

Поиск по ресурсу — это запрос `"<запрос> site:<domain>"`. Без пути ddgs отдаёт выдачу по всем
регионам: «концерт Владивосток site:gorodzovet.ru» приносил события Мурманска и Казани, и
бюджет страниц сгорал на них. Поэтому у таких ресурсов задан `url_contains`.

Общий поиск (`general_queries`) состоит из `news` за сутки и `text` за неделю, `{city}`
подставляется из `cities`. Формулировки подобраны вручную: ddgs чувствителен к словам. Например,
`выставка Владивосток` даёт 0 результатов, а `выставка Владивосток 2026` и
`выставка Владивосток музей` — по 7–8. Пустая выдача на части запросов — норма, не ошибка.

Площадку рядом с событием указывают афиши `afishagorodov.ru`, `vladivostok.ticketland.ru` и
`vl.kassir.ru`, они дают `venue` почти для всех событий. `vl.ru` не используется по решению
заказчика. Почему остальные домены в `blocked_domains`, написано в комментариях `config.yaml`.

## Быстрая проверка

Скопируйте `config.yaml`, поставьте `pages_per_run: 3` и `general: false`, затем:

```bash
uv run --env-file .env event-agent --config копия.yaml run > out.json

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

Повторный прогон должен вернуть только новое и изменившееся. Чтобы начать с чистого листа,
удалите `data/state.json`. Страницы со статусом `fetch_failed` / `llm_failed` и счётчиком
`fails` будут повторены, если `until` не в будущем (иначе они в карантине).
