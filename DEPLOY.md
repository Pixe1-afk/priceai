# Deploy на Render / Railway / Fly.io

## Render (рекомендуется, проще всего)

1. Залейте проект в GitHub-репозиторий.
2. Зайдите на https://render.com → New → Web Service → подключите репозиторий.
3. Render автоматически подхватит `render.yaml` и развернёт.
4. Через 2-3 минуты сайт будет доступен по адресу `https://<имя>.onrender.com`.

Бесплатный план: сервис «засыпает» после 15 минут простоя, первый запрос после сна — ~30 сек.

## Railway

1. https://railway.app → New Project → Deploy from GitHub.
2. Railway сам найдёт `Procfile` и установит зависимости.
3. В Variables добавьте `FLASK_DEBUG=0`.

## Fly.io (через Docker)

```
fly launch                  # сам найдёт Dockerfile
fly secrets set FLASK_DEBUG=0
fly deploy
```

## Переменные окружения

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `FLASK_DEBUG` | `0` | `1` — режим разработки (не использовать в проде!) |
| `PORT` | `5000` | Порт, на котором слушает приложение (PaaS выставляет автоматически) |
| `ALLOWED_ORIGINS` | пусто | Через запятую — какие origin-ы могут делать CORS-запросы. Пусто = same-origin only. |
| `RATE_LIMIT_PER_MIN` | `120` | Лимит запросов в минуту с одного IP |

## Локальный запуск (Windows)

```
pip install -r requirements.txt
python app.py             # waitress на порту 5000
```

Для dev-режима с auto-reload:
```
set FLASK_DEBUG=1
python app.py
```

## Что было сделано для безопасности

- Удалены все админ-эндпоинты (`/api/admin/*`) и страница `/admin`.
- `debug=False` по умолчанию, включается только через env.
- Security headers: HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy.
- Rate-limit на IP (120 req/min по умолчанию).
- CORS закрыт same-origin, либо ограничен `ALLOWED_ORIGINS`.
- Все исходящие HTTPS-запросы проверяют сертификаты.
- Локальные файлы данных (`prices_*.json`, `wfp_cache.json`) исключены из git.

## Чек-лист перед публикацией

- [ ] `git status` — нет следов админ-эндпоинтов и `12345` в коде
- [ ] PaaS-провайдер не выставляет `FLASK_DEBUG=1`
- [ ] Если используется свой домен — поставить `ALLOWED_ORIGINS=https://your.domain`
- [ ] HTTPS включён (PaaS обычно сами выдают сертификат)
