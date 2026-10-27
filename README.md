# RetroTube Terminal Client

CLI-клиент для API [retrotube.net](https://retrotube.net) (спецификация: `retrotube_openapi.json`).

**Зависимостей нет** — только стандартная библиотека Python 3.8+.
`requirements.txt` присутствует формально (пустой, с пояснением) — для привычного
`pip install -r requirements.txt`; он ничего не устанавливает.

## Запуск

```
python retrotube_cli.py            # интерактивный режим
python retrotube_cli.py /feed      # одна команда и выход
```

Вход в **свой** аккаунт: `/login <username>` (пароль вводится скрыто).
Сессия сохраняется в `~/.retrotube_session.json` (cookie `refreshToken`, живёт 1 год),
при следующем запуске логиниться не нужно. Регистрация — через сайт (там капча и почта).

## Основные команды

| Команда | Что делает |
|---|---|
| `/feed [стр] [sort] [категория]` | каталог видео (`newest`/`popular`/`oldest`) |
| `/featured`, `/categories` | рекомендуемые, категории |
| `/search [users] <запрос>` | поиск видео/пользователей |
| `/random [open]` | случайное видео из каталога (`open` — сразу открыть в браузере) |
| `/video <n\|ссылка\|url-id>` | детали видео, выбор «текущего» |
| `/watch [n\|ссылка]` | открыть видео в браузере |
| `/comments [стр]`, `/comment <текст>`, `/reply <n> <текст>` | комментарии |
| `/like`, `/dislike`, `/unrate` `[n\|video "ссылка"]` | ваша оценка видео (одно действие от вашего аккаунта) |
| `/channel <user>`, `/sub`, `/unsub`, `/subcheck` | каналы и подписки |
| `/subsfeed` | новые видео из ваших подписок |
| `/notifications`, `/read` | уведомления |
| `/playlists`, `/mkplaylist`, `/playlist <id>`, `/addpl` | плейлисты |
| `/community <user>`, `/post <текст>` | сообщество |
| `/report [n\|ссылка] <reason> <info>` | жалоба (`spam\|nudity\|hate\|harassment\|copyright\|other`) |
| `/limit` | остаток rate-limit (у API лимит: 10 запросов на маршрут за ~15 мин, 1000 на IP) |

Вместо номера из списка (`n`) почти везде можно подставить ссылку
`https://retrotube.net/watch?v=…` или url-ид; работает стиль
`/dislike video "https://retrotube.net/watch?v=XXXX"`.

Пример сессии:

```
rt[гость]> /login ilya
  Пароль для @ilya: ********
  Вход выполнен: @ilya
rt[ilya]> /feed popular Gaming
rt[ilya]> /video 3
rt[ilya]> /like
rt[ilya]> /comment годный выпуск!
```

## Примечания об API

- Авторизация — cookie: `accessToken` живёт 5 секунд и продлевается сервером
  автоматически, `refreshToken` — 1 год; клиент хранит оба с учётом Max-Age.
- `ref` для реакций/комментариев/жалоб — **ObjectId** (`_id` видео), не url-ид.
- Тексты на сервере HTML-escaped — клиент раскодирует их при показе.
- Детальный ответ видео не содержит `formattedLength` — клиент считает из секунд.
