#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RetroTube Terminal Client — CLI-клиент для API retrotube.net.

Зависимости: только стандартная библиотека Python 3.8+.
Сессия хранится в ~/.retrotube_session.json (cookie refreshToken, живёт 1 год).

Запуск:  python retrotube_cli.py            — интерактивный режим (REPL)
         python retrotube_cli.py /feed      — выполнить одну команду и выйти

Справка по командам: /help
"""

import html
import json
import os
import random
import re
import shlex
import shutil
import sys
import time
import uuid
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

BASE = "https://retrotube.net/api/v1"
SITE = "https://retrotube.net"
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".retrotube_session.json")
UA = "retrotube-cli/1.0 (terminal client)"
TIMEOUT = 25


# --------------------------------------------------------------------------- #
#  Сессия (cookie-хранилище с учётом Max-Age)                                  #
# --------------------------------------------------------------------------- #

class Session:
    """Хранит cookie как {имя: (значение, expires_ts)} и умеет строить заголовок Cookie."""

    def __init__(self):
        self.cookies = {}
        self.load()

    def load(self):
        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    self.cookies = json.load(f)
            except (OSError, ValueError):
                self.cookies = {}

    def save(self):
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cookies, f)
        except OSError:
            pass

    def update_from_response(self, resp):
        """Разбирает все Set-Cookie из ответа. Возвращает имена установленных cookie."""
        set_names = []
        for raw in resp.headers.get_all("Set-Cookie") or []:
            sc = SimpleCookie()
            try:
                sc.load(raw)
            except Exception:
                continue
            for name, morsel in sc.items():
                max_age = morsel["max-age"]
                expires = time.time() + int(max_age) if max_age and max_age.lstrip("-").isdigit() else None
                if expires is not None and expires <= time.time():
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = [morsel.value, expires]
                    set_names.append(name)
        if set_names:
            self.save()
        return set_names

    def header(self):
        parts = []
        for name, (value, expires) in list(self.cookies.items()):
            if expires is not None and expires <= time.time():
                continue
            parts.append("%s=%s" % (name, value))
        return "; ".join(parts)

    def clear(self):
        self.cookies = {}
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  HTTP                                                                       #
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    def __init__(self, status, code=None, message=None):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        parts = ["HTTP %s" % self.status]
        if self.code:
            parts.append(self.code)
        if self.message:
            parts.append(self.message)
        return ": ".join(parts)


class Client:
    def __init__(self, session):
        self.session = session
        self.last_rate_limit = {}

    def request(self, method, path, params=None, form=None, json_body=None,
                multipart_fields=None, raw_response=False):
        url = BASE + path
        if params:
            url += "?" + urlparse.urlencode(params, doseq=True)

        headers = {"User-Agent": UA, "Accept": "application/json"}
        cookie = self.session.header()
        if cookie:
            headers["Cookie"] = cookie

        data = None
        if form is not None:
            data = urlparse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif multipart_fields is not None:
            data, ctype = self._build_multipart(multipart_fields)
            headers["Content-Type"] = ctype

        req = urlrequest.Request(url, data=data, headers=headers, method=method)
        last_err = None
        for attempt in (1, 2):  # сервер иногда рвёт TLS-рукопожатие — один ретрай
            try:
                resp = urlrequest.urlopen(req, timeout=TIMEOUT)
                last_err = None
                break
            except urlerror.HTTPError as e:
                body = e.read()
                code, message = None, None
                try:
                    err = json.loads(body.decode("utf-8"))
                    code = err.get("code") or err.get("error")
                    message = err.get("message")
                except Exception:
                    message = body.decode("utf-8", "replace")[:200] or None
                rl = self._rate_limit_from_headers(e.headers)
                if rl:
                    self.last_rate_limit = rl
                raise ApiError(e.code, code, message)
            except urlerror.URLError as e:
                last_err = ApiError(0, "NETWORK", str(getattr(e, "reason", e)))
                if data is not None:
                    break  # повторять небезопасные (POST/…) запросы не будем
                time.sleep(0.7)
        if last_err is not None:
            raise last_err

        self.last_rate_limit = self._rate_limit_from_headers(resp.headers) or self.last_rate_limit
        self.session.update_from_response(resp)
        body = resp.read()
        if raw_response:
            return body
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            return {"_raw": body.decode("utf-8", "replace")[:500]}

    @staticmethod
    def _rate_limit_from_headers(headers):
        if not headers:
            return None
        rl = {}
        for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"):
            v = headers.get(h)
            if v is not None:
                rl[h] = v
        return rl or None

    @staticmethod
    def _build_multipart(fields):
        boundary = "----rtcli" + uuid.uuid4().hex
        out = b""
        for name, value in fields.items():
            out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n" % (boundary, name)).encode("utf-8")
            out += str(value).encode("utf-8") + b"\r\n"
        out += ("--%s--\r\n" % boundary).encode("utf-8")
        return out, "multipart/form-data; boundary=%s" % boundary


# --------------------------------------------------------------------------- #
#  Форматирование                                                             #
# --------------------------------------------------------------------------- #

WIDTH = min(shutil.get_terminal_size((100, 24)).columns, 120)


def clean(text):
    """Сервер отдаёт HTML-escaped текст (&#x2F; и т.п.) — раскодируем для показа."""
    return html.unescape(text or "")


def trunc(s, n):
    s = clean(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_date(iso):
    if not iso:
        return "?"
    try:
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso[:10]


def fmt_count(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1000:
        return "%.1fK" % (n / 1000)
    return str(n)


def fmt_length(v):
    if v.get("formattedLength"):
        return v["formattedLength"]
    try:
        sec = int(float(v.get("length") or 0))
        return "%d:%02d" % (sec // 60, sec % 60)
    except (TypeError, ValueError):
        return "?"


def media_url(path, kind):
    """thumbnailPath/avatarPath/filePath — относительные пути => /uploads/<kind>/<path>."""
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return "%s/uploads/%s/%s" % (SITE, kind, path.lstrip("/"))


def video_watch_url(v):
    return "%s/watch?v=%s" % (SITE, v.get("url", ""))


def print_video_row(i, v):
    author = (v.get("uploadedBy") or {}).get("username", "?")
    sens = " [18+]" if v.get("isSensitive") else ""
    cat = "/%s" % v["category"] if v.get("category") else ""
    print("  %3d. %s%s" % (i, trunc(v.get("title", "?"), WIDTH - 12), sens))
    print("       @%s%s | %s | %s просм. | +%s/-%s | %s" % (
        author, cat, fmt_length(v), fmt_count(v.get("views")), fmt_count(v.get("likes")),
        fmt_count(v.get("dislikes")), fmt_date(v.get("createdAt"))))
    print("       id=%s  %s" % (v.get("url", "?"), video_watch_url(v)))


def print_video_details(v):
    author = v.get("uploadedBy") or {}
    line = "=" * WIDTH
    print(line)
    print(trunc(v.get("title", "?"), WIDTH))
    print(line)
    print("  Канал    : @%s (%s), подписчиков: %s" % (
        author.get("username", "?"), author.get("displayName", "?"),
        fmt_count(author.get("subscribers"))))
    if v.get("category"):
        print("  Категория: %s   Теги: %s" % (v["category"], trunc(v.get("tags", ""), 60)))
    print("  Длительность: %s   Опубликовано: %s" % (
        fmt_length(v), fmt_date(v.get("createdAt"))))
    print("  Статистика: %s просм. | +%s / -%s%s" % (
        fmt_count(v.get("views")), fmt_count(v.get("likes")),
        fmt_count(v.get("dislikes")), "   [18+]" if v.get("isSensitive") else ""))
    print("  Ссылка   : %s" % video_watch_url(v))
    mp4 = media_url(v.get("filePath"), "videos")
    if mp4:
        print("  Видео(mp4): %s" % mp4)
    thumb = media_url(v.get("thumbnailPath"), "thumbnails")
    if thumb:
        print("  Обложка  : %s" % thumb)
    print("  ObjectId : %s (ref для жалоб)" % v.get("_id", "?"))
    desc = clean(v.get("description") or "").strip()
    if desc:
        print("-" * WIDTH)
        print(trunc(desc, WIDTH * 4))
    print(line)


def print_comment_row(i, c):
    user = c.get("user") or {}
    pin = " [pin]" if c.get("isPinned") else ""
    edited = " (изм.)" if c.get("isEdited") else ""
    print("  %3d. @%s%s%s | +%s/-%s | ответов: %s | %s" % (
        i, user.get("username", "?"), pin, edited,
        fmt_count(c.get("likes")), fmt_count(c.get("dislikes")),
        fmt_count(c.get("replies")), fmt_date(c.get("createdAt"))))
    text = clean(c.get("text") or "").strip()
    for chunk_start in range(0, min(len(text), 600), WIDTH - 8):
        print("       " + text[chunk_start:chunk_start + WIDTH - 8])


def print_paginated_footer(data, page=1):
    if isinstance(data, dict) and "totalPages" in data:
        print("  --- страница %s/%s%s ---" % (
            page, data.get("totalPages", "?"),
            ", последняя" if data.get("isLastPage") else ""))


# --------------------------------------------------------------------------- #
#  Приложение                                                                 #
# --------------------------------------------------------------------------- #

class App:
    def __init__(self):
        self.session = Session()
        self.client = Client(self.session)
        self.last_videos = []      # последний показанный список видео
        self.last_comments = []    # последний список комментариев
        self.current_video = None  # выбранное видео (полный объект)
        self.me = None             # данные своей сессии

    # ------------------------------------------------------------------ API

    def api(self, method, path, **kw):
        return self.client.request(method, path, **kw)

    def refresh_me(self):
        try:
            data = self.api("GET", "/auth/session")
            self.me = data.get("user") or data
            return self.me
        except ApiError:
            self.me = None
            return None

    def require_auth(self):
        if self.me is None:
            self.refresh_me()
        if self.me is None:
            print("  Не выполнен вход. Используйте: /login <username>")
            return False
        return True

    # ------------------------------------------------------ выбор видео/ссылки

    def resolve_video(self, args):
        """Аргумент: номер в последнем списке | ссылка watch?v= | url-id.
        Допускает слово 'video' первым токеном (стиль /dislike video "ссылка")."""
        args = list(args)
        if args and args[0].lower() == "video":
            args = args[1:]
        if not args:
            if self.current_video:
                return self.current_video
            print("  Нет выбранного видео. Укажите номер из списка, ссылку или id.")
            return None
        token = args[0].strip().strip('"\'')
        # номер из последнего списка
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(self.last_videos):
                return self.last_videos[idx - 1]
            print("  Нет видео №%d в последнем списке (там %d шт.)." % (idx, len(self.last_videos)))
            return None
        vid = extract_video_id(token)
        if not vid:
            print("  Не понял аргумент: %r. Нужен номер, ссылка /watch?v=... или url-id." % token)
            return None
        try:
            return self.api("GET", "/videos/%s" % vid)
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return None

    # -------------------------------------------------------------- команды

    def cmd_login(self, args):
        if not args:
            print("  Использование: /login <username>")
            return
        username = args[0]
        import getpass
        password = getpass.getpass("  Пароль для @%s: " % username)
        if not password:
            print("  Отменено.")
            return
        try:
            self.api("POST", "/auth/login", form={"username": username, "password": password})
        except ApiError as e:
            print("  Вход не удался: %s" % e)
            return
        me = self.refresh_me()
        if me:
            print("  Вход выполнен: @%s (display: %s, premium: %s)" % (
                me.get("username"), me.get("displayName", "?"),
                "да" if me.get("hasPremium") else "нет"))
        else:
            print("  Cookie получены, но /auth/session не подтвердил сессию.")

    def cmd_logout(self, args):
        try:
            self.api("POST", "/auth/logout")
        except ApiError as e:
            print("  (сервер ответил: %s)" % e)
        self.session.clear()
        self.me = None
        print("  Выход выполнен, локальная сессия удалена.")

    def cmd_whoami(self, args):
        me = self.refresh_me()
        if not me:
            print("  Гость (не авторизован).")
            return
        print("  @%s | display: %s | id: %s | verified: %s | premium: %s | roles: %s" % (
            me.get("username"), me.get("displayName", "?"), me.get("_id", "?"),
            "да" if me.get("isVerified") else "нет",
            "да" if me.get("hasPremium") else "нет",
            ",".join(me.get("roles") or []) or "-"))

    def cmd_feed(self, args):
        page, sort, category = 1, "newest", None
        for a in args:
            if a.isdigit():
                page = int(a)
            elif a in ("newest", "popular", "oldest"):
                sort = a
            elif a.startswith("category="):
                category = a.split("=", 1)[1].capitalize()
            elif a.capitalize() in CATEGORIES:
                category = a.capitalize()
        params = {"page": page, "limit": 15, "sort": sort}
        if category:
            params["category"] = category
        try:
            data = self.api("GET", "/videos", params=params)
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        self.last_videos = data.get("items", [])
        title = "Каталог (%s%s)" % (sort, ", " + category if category else "")
        print("  %s — страница %d/%s:" % (title, page, data.get("totalPages", "?")))
        for i, v in enumerate(self.last_videos, 1):
            print_video_row(i, v)
        print_paginated_footer(data, page)

    def cmd_featured(self, args):
        try:
            items = self.api("GET", "/videos/featured")
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        if not isinstance(items, list):
            items = items.get("items", [])
        self.last_videos = items
        print("  Рекомендуемые (%d шт.):" % len(items))
        for i, v in enumerate(items, 1):
            print_video_row(i, v)

    def cmd_random(self, args):
        # Прямого /videos/random у API нет — сервер трактует "random" как url-ид
        # видео и отвечает 400 URL_INVALID, поэтому выбираем из каталога.
        open_browser = any(a.lower() in ("open", "watch") for a in args)
        try:
            first = self.api("GET", "/videos", params={"page": 1, "limit": 25, "sort": "newest"})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        total = first.get("totalPages") or 1
        page = random.randint(1, total)
        try:
            data = first if page == 1 else self.api(
                "GET", "/videos", params={"page": page, "limit": 25, "sort": "newest"})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        items = data.get("items") or []
        if not items:
            print("  Страница %d оказалась пуста, попробуйте ещё раз." % page)
            return
        v = random.choice(items)
        self.last_videos = items
        self.current_video = v
        print("  Случайное видео (страница %d/%s):" % (page, total))
        print_video_details(v)
        if open_browser:
            print("  Открываю: %s" % video_watch_url(v))
            webbrowser.open(video_watch_url(v))

    def cmd_categories(self, args):
        try:
            data = self.api("GET", "/videos/categories")
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        print("  Категории: " + ", ".join(data.get("categories", [])))

    def cmd_search(self, args):
        if not args:
            print("  Использование: /search <запрос>  |  /search users <запрос>")
            return
        stype = "videos"
        if args[0].lower() in ("users", "videos"):
            stype = args.pop(0).lower()
        query = " ".join(args).strip('"\'')
        try:
            data = self.api("GET", "/search", params={
                "query": query, "type": stype, "page": 1, "limit": 15})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        items = data.get("items", data if isinstance(data, list) else [])
        if stype == "users":
            self.last_videos = []
            print("  Пользователи по запросу %r:" % query)
            for i, u in enumerate(items, 1):
                print("  %3d. @%s (%s) | подписчиков: %s" % (
                    i, u.get("username", "?"), trunc(u.get("displayName", ""), 30),
                    fmt_count(u.get("subscribers"))))
        else:
            self.last_videos = items
            print("  Видео по запросу %r:" % query)
            for i, v in enumerate(items, 1):
                print_video_row(i, v)
        print_paginated_footer(data if isinstance(data, dict) else {}, 1)

    def cmd_video(self, args):
        v = self.resolve_video(args)
        if not v:
            return
        self.current_video = v
        print_video_details(v)

    def cmd_watch(self, args):
        v = self.resolve_video(args)
        if not v:
            return
        self.current_video = v
        url = video_watch_url(v)
        print("  Открываю: %s" % url)
        webbrowser.open(url)

    def cmd_comments(self, args):
        if not self.current_video:
            print("  Сначала выберите видео: /video <номер|ссылка>")
            return
        page = 1
        for a in args:
            if a.isdigit():
                page = int(a)
        try:
            data = self.api("GET", "/comments", params={
                "ref": self.current_video["_id"], "refType": "Video",
                "page": page, "limit": 15, "sort": "newest"})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        self.last_comments = data.get("items", [])
        print("  Комментарии к %r — страница %d/%s:" % (
            trunc(self.current_video.get("title", ""), 40), page, data.get("totalPages", "?")))
        if not self.last_comments:
            print("  (пусто)")
        for i, c in enumerate(self.last_comments, 1):
            print_comment_row(i, c)
        print_paginated_footer(data, page)

    def cmd_comment(self, args):
        if not self.require_auth():
            return
        if not self.current_video:
            print("  Сначала выберите видео: /video <номер|ссылка>")
            return
        text = " ".join(args).strip()
        if not text:
            print("  Использование: /comment <текст>")
            return
        try:
            self.api("POST", "/comments",
                     params={"ref": self.current_video["_id"], "refType": "Video"},
                     multipart_fields={"text": text})
            print("  Комментарий отправлен (id сервер не возвращает — /comments покажет).")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_reply(self, args):
        if not self.require_auth():
            return
        if len(args) < 2 or not args[0].isdigit():
            print("  Использование: /reply <номер комментария> <текст>")
            return
        idx = int(args[0])
        if not 1 <= idx <= len(self.last_comments):
            print("  Нет комментария №%d (сделайте /comments)." % idx)
            return
        comment = self.last_comments[idx - 1]
        text = " ".join(args[1:]).strip()
        try:
            self.api("POST", "/comments/%s/replies" % comment["_id"],
                     multipart_fields={"text": text})
            print("  Ответ отправлен.")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def _rate_video(self, args, action):
        if not self.require_auth():
            return
        v = self.resolve_video(args)
        if not v:
            return
        self.current_video = v
        verbs = {"like": "Лайк", "dislike": "Дизлайк", "unrate": "Оценка снята"}
        try:
            if action == "unrate":
                self.api("DELETE", "/engagement/rating",
                         params={"ref": v["_id"], "refType": "Video"})
            else:
                self.api("POST", "/engagement/%s" % action,
                         params={"ref": v["_id"], "refType": "Video"})
            print("  %s: %r (@%s)" % (verbs[action], trunc(v.get("title", ""), 50),
                                      (v.get("uploadedBy") or {}).get("username", "?")))
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_like(self, args):
        self._rate_video(args, "like")

    def cmd_dislike(self, args):
        self._rate_video(args, "dislike")

    def cmd_unrate(self, args):
        self._rate_video(args, "unrate")

    def _channel_id(self, username):
        try:
            prof = self.api("GET", "/users/%s" % username)
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return None
        return prof.get("_id"), prof

    def cmd_channel(self, args):
        if not args:
            print("  Использование: /channel <username>")
            return
        username = args[0].lstrip("@")
        try:
            prof = self.api("GET", "/users/%s" % username)
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        print("=" * WIDTH)
        print("  @%s — %s%s" % (prof.get("username"), prof.get("displayName", ""),
                               "  [badge]" if prof.get("badgePath") else ""))
        print("  Подписчиков: %s | На RetroTube с %s" % (
            fmt_count(prof.get("subscribers")), fmt_date(prof.get("createdAt"))))
        bio = (prof.get("bio") or "").strip()
        if bio:
            print("  Bio: %s" % trunc(bio, WIDTH - 8))
        try:
            stats = self.api("GET", "/users/%s/stats" % username)
            print("  Статистика: видео %s | просм. %s | +%s/-%s | комм. %s" % (
                stats.get("videos"), fmt_count(stats.get("views")),
                fmt_count(stats.get("likes")), fmt_count(stats.get("dislikes")),
                fmt_count(stats.get("comments"))))
        except ApiError:
            pass
        print("=" * WIDTH)
        try:
            vids = self.api("GET", "/users/%s/videos" % username,
                            params={"page": 1, "limit": 10})
            self.last_videos = vids.get("items", [])
            print("  Видео (@%s):" % username)
            for i, v in enumerate(self.last_videos, 1):
                print_video_row(i, v)
            print_paginated_footer(vids, 1)
        except ApiError as e:
            print("  (список видео недоступен: %s)" % e)

    def cmd_sub(self, args):
        self._sub_action(args, subscribe=True)

    def cmd_unsub(self, args):
        self._sub_action(args, subscribe=False)

    def _sub_action(self, args, subscribe):
        if not self.require_auth():
            return
        if not args:
            print("  Использование: /%s <username>" % ("sub" if subscribe else "unsub"))
            return
        username = args[0].lstrip("@")
        resolved = self._channel_id(username)
        if not resolved:
            return
        channel_id, _prof = resolved
        try:
            if subscribe:
                self.api("POST", "/engagement/%s/subscribers" % channel_id)
                print("  Подписка на @%s оформлена." % username)
            else:
                self.api("DELETE", "/engagement/%s/subscribers" % channel_id)
                print("  Вы отписались от @%s." % username)
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_subcheck(self, args):
        if not self.require_auth():
            return
        if not args:
            print("  Использование: /subcheck <username>")
            return
        username = args[0].lstrip("@")
        resolved = self._channel_id(username)
        if not resolved:
            return
        channel_id, _prof = resolved
        try:
            state = self.api("GET", "/engagement/%s/subscription" % channel_id)
            print("  Состояние подписки на @%s: %s" % (username, json.dumps(state, ensure_ascii=False)))
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_subsfeed(self, args):
        if not self.require_auth():
            return
        page = next((int(a) for a in args if a.isdigit()), 1)
        try:
            data = self.api("GET", "/users/%s/subscriptions/videos" % self.me["username"],
                            params={"page": page, "limit": 15})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        self.last_videos = data.get("items", [])
        print("  Видео из подписок — страница %d:" % page)
        for i, v in enumerate(self.last_videos, 1):
            print_video_row(i, v)
        print_paginated_footer(data, page)

    def cmd_notifications(self, args):
        if not self.require_auth():
            return
        page = next((int(a) for a in args if a.isdigit()), 1)
        try:
            data = self.api("GET", "/users/%s/notifications" % self.me["username"],
                            params={"page": page})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        items = data.get("items", [])
        print("  Уведомления — страница %d:" % page)
        for n in items:
            caller = (n.get("caller") or {}).get("username", "")
            who = " от @%s" % caller if caller else ""
            read = "" if n.get("isRead") else " [новое]"
            print("  %s | %s%s%s | %s" % (
                fmt_date(n.get("createdAt")), n.get("type", "?"), who, read,
                trunc(n.get("message") or "", 70)))
        if not items:
            print("  (пусто)")
        print_paginated_footer(data, page)

    def cmd_read(self, args):
        if not self.require_auth():
            return
        try:
            self.api("POST", "/users/%s/notifications" % self.me["username"],
                     params={"read": "true"})
            print("  Уведомления помечены прочитанными.")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_playlists(self, args):
        if not self.require_auth():
            return
        page = next((int(a) for a in args if a.isdigit()), 1)
        try:
            data = self.api("GET", "/users/%s/playlists" % self.me["username"],
                            params={"page": page, "limit": 15})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        items = data.get("items", [])
        print("  Плейлисты — страница %d:" % page)
        for p in items:
            stats = p.get("stats") or {}
            print("  - %s | видео: %s | %s | id=%s" % (
                p.get("name", "?"), stats.get("videos", "?"),
                p.get("visibility", "?"), p.get("_id", "?")))
        if not items:
            print("  (пусто)")
        print_paginated_footer(data, page)

    def cmd_mkplaylist(self, args):
        if not self.require_auth():
            return
        if not args:
            print("  Использование: /mkplaylist <название> [public|unlisted|private]")
            return
        name = args[0].strip('"\'')
        visibility = args[1] if len(args) > 1 else "public"
        if visibility not in ("public", "unlisted", "private"):
            print("  visibility должен быть public|unlisted|private")
            return
        try:
            self.api("POST", "/playlists",
                     multipart_fields={"name": name, "description": "", "visibility": visibility})
            print("  Плейлист создан. Список: /playlists")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_playlist(self, args):
        if not args or not re.match(r"^[0-9a-f]{24}$", args[0]):
            print("  Использование: /playlist <playlistId (24 hex)>  (id из /playlists)")
            return
        try:
            p = self.api("GET", "/playlists/%s" % args[0])
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        print("  Плейлист %r (%s):" % (p.get("name"), p.get("visibility")))
        items = p.get("items") or []
        videos = [it.get("video") for it in items if it.get("video")]
        self.last_videos = videos
        for i, v in enumerate(videos, 1):
            print_video_row(i, v)
        if not videos:
            print("  (пусто)")

    def cmd_addpl(self, args):
        if not self.require_auth():
            return
        if len(args) < 2:
            print("  Использование: /addpl <playlistId> <номер видео|url-id|ссылка>")
            return
        pid = args[0]
        v = self.resolve_video(args[1:])
        if not v:
            return
        try:
            self.api("POST", "/playlists/%s/videos/%s" % (pid, v["url"]))
            print("  Добавлено в плейлист.")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_community(self, args):
        if not args:
            print("  Использование: /community <username> [страница]")
            return
        username = args[0].lstrip("@")
        page = next((int(a) for a in args[1:] if a.isdigit()), 1)
        try:
            data = self.api("GET", "/community/%s/posts" % username,
                            params={"page": page, "limit": 10})
        except ApiError as e:
            print("  Ошибка: %s" % e)
            return
        print("  Посты @%s — страница %d/%s:" % (username, page, data.get("totalPages", "?")))
        for p in data.get("items", []):
            print("  %s | +%s/-%s | id=%s" % (
                fmt_date(p.get("createdAt")), fmt_count(p.get("likes")),
                fmt_count(p.get("dislikes")), p.get("_id", "?")))
            content = clean(p.get("content") or "").strip()
            for cs in range(0, min(len(content), 400), WIDTH - 8):
                print("    " + content[cs:cs + WIDTH - 8])
        if not data.get("items"):
            print("  (пусто)")
        print_paginated_footer(data, page)

    def cmd_post(self, args):
        if not self.require_auth():
            return
        content = " ".join(args).strip()
        if not content:
            print("  Использование: /post <текст поста в сообщество>")
            return
        try:
            self.api("POST", "/community/posts", json_body={"content": content})
            print("  Пост опубликован.")
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_report(self, args):
        if not self.current_video and not args:
            print("  Использование: /report [номер|ссылка] <reason> <info>")
            return
        reasons = ("spam", "nudity", "hate", "harassment", "copyright", "other")
        # отделяем опциональный селектор видео
        vref_args = []
        rest = list(args)
        if args and (args[0].isdigit() or extract_video_id(args[0]) or args[0].lower() == "video"):
            vref_args, rest = args[:1], args[1:]
        if not rest or rest[0] not in reasons:
            print("  Причина обязательна: %s" % ", ".join(reasons))
            print("  Использование: /report [номер|ссылка] <reason> <пояснение>")
            return
        reason = rest[0]
        info = " ".join(rest[1:]).strip()
        if not info:
            print("  Требуется пояснение (info) — сервер отвечает INFO_REQUIRED.")
            return
        v = self.resolve_video(vref_args) if vref_args else self.current_video
        if not v:
            print("  Нет видео для жалобы. Укажите номер/ссылку или выберите /video ...")
            return
        try:
            self.api("POST", "/reports", json_body={
                "ref": v["_id"], "refType": "Video", "reason": reason, "info": info})
            print("  Жалоба на %r отправлена (%s)." % (trunc(v.get("title", ""), 40), reason))
        except ApiError as e:
            print("  Ошибка: %s" % e)

    def cmd_limit(self, args):
        rl = self.client.last_rate_limit
        if not rl:
            print("  Данных о rate-limit ещё нет.")
            return
        for k, val in rl.items():
            print("  %s: %s" % (k, val))

    def cmd_help(self, args):
        print("""  Команды RetroTube CLI:

  Аккаунт:
    /login <username>            вход (пароль скрыто)
    /logout                      выход
    /whoami                      текущая сессия
  Просмотр:
    /feed [стр] [newest|popular|oldest] [категория]   каталог видео
    /featured                    рекомендуемые
    /categories                  список категорий
    /search [users] <запрос>     поиск видео или пользователей
    /random [open]               случайное видео (open — сразу в браузере)
    /video <n|ссылка|url-id>     детали видео (n — номер из последнего списка)
    /watch [n|ссылка]            открыть видео в браузере
  Общение:
    /comments [стр]              комментарии к выбранному видео
    /comment <текст>             написать комментарий
    /reply <n> <текст>           ответить на комментарий №n
    /community <user> [стр]      посты пользователя в сообществе
    /post <текст>                создать пост в сообществе
  Реакции (ваш аккаунт, одно действие):
    /like    [n|video "ссылка"]  лайк
    /dislike [n|video "ссылка"]  дизлайк
    /unrate  [n|video "ссылка"]  снять свою оценку
  Каналы:
    /channel <username>          профиль + видео канала
    /sub <username>              подписаться
    /unsub <username>            отписаться
    /subcheck <username>         состояние подписки
    /subsfeed [стр]              новые видео из ваших подписок
  Прочее:
    /notifications [стр]         уведомления
    /read                        пометить уведомления прочитанными
    /playlists [стр]             ваши плейлисты
    /mkplaylist <имя> [vis]      создать плейлист (public|unlisted|private)
    /playlist <id>               содержимое плейлиста
    /addpl <id> <n|ссылка>       добавить видео в плейлист
    /report [n|ссылка] <reason> <info>   жалоба (spam|nudity|hate|harassment|copyright|other)
    /limit                       лимиты запросов (X-RateLimit)
    /help | /exit

  Вместо номера n можно всегда подставить ссылку вида https://retrotube.net/watch?v=... ,
  допустим стиль: /dislike video "https://retrotube.net/watch?v=XXXX".""")

    # ---------------------------------------------------------------- REPL

    def dispatch(self, line):
        line = line.strip()
        if not line:
            return True
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            tokens = line.split()
        cmd = tokens[0].lstrip("/").lower()
        args = tokens[1:]
        handler = getattr(self, "cmd_%s" % cmd, None)
        if cmd in ("exit", "quit", "q", "bye"):
            return False
        if handler is None:
            print("  Неизвестная команда: /%s (см. /help)" % cmd)
            return True
        try:
            handler(args)
        except ApiError as e:
            print("  Ошибка API: %s" % e)
        except KeyboardInterrupt:
            print("  (прервано)")
        return True

    def prompt(self):
        who = self.me["username"] if self.me else "гость"
        try:
            return "rt[%s]> " % who
        except Exception:
            return "rt> "

    def run(self):
        me = self.refresh_me()
        if me:
            print("  Сессия восстановлена: @%s" % me.get("username"))
        else:
            print("  Гость. Вход: /login <username>. Справка: /help.")
        while True:
            try:
                line = input(self.prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                break
            except UnicodeDecodeError as e:
                print("  (не удалось декодировать ввод: %s)" % e)
                continue
            if not self.dispatch(line):
                break
        print("  Пока!")


CATEGORIES = ("Gaming", "Music", "Art", "Comedy", "Entertainment", "News",
              "Sports", "Technology", "Travel", "Tutorial", "Animals", "Science")

WATCH_RE = re.compile(r"watch\?v=([A-Za-z0-9_-]{5,20})")


def extract_video_id(token):
    """url-id из ссылки /watch?v=..., youtu-подобной ссылки или голого id."""
    token = (token or "").strip().strip('"\'')
    m = WATCH_RE.search(token)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{8,15}", token):
        return token
    return None


def main():
    # UTF-8 вывод в консоли Windows
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    app = App()
    if len(sys.argv) > 1:
        ok = app.dispatch(" ".join(sys.argv[1:]))
        sys.exit(0 if ok else 1)
    app.run()


if __name__ == "__main__":
    main()
