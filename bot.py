"""
Бот канала «С ударом!» (@s_ydarom).
Каждый запуск: собирает свежие статьи из RSS, выбирает самую обсуждаемую
историю, пересказывает её по-русски и публикует один пост в канал.
"""
import os
import re
import json
import time
import html
import calendar
from concurrent.futures import ThreadPoolExecutor
import datetime as dt

import requests
import feedparser
from bs4 import BeautifulSoup

# ---------------- НАСТРОЙКИ ----------------
CHANNEL = "@s_ydarom"
STATE_FILE = "posted.json"
MAX_AGE_HOURS = 12          # берём только статьи не старше 12 часов
MAX_CANDIDATES = 60         # сколько заголовков показываем ИИ
UA = {"User-Agent": "Mozilla/5.0 (compatible; SUdaromBot/1.0)"}

# Источники: название -> RSS. Если какой-то перестанет работать, бот его просто пропустит.
FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "BBC Sport (Шотландия)": "https://feeds.bbci.co.uk/sport/football/scottish/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
    "Sky Sports": "https://www.skysports.com/rss/12040",
    "ESPN": "https://www.espn.com/espn/rss/soccer/news",
    "Football365": "https://www.football365.com/feed",
    "Marca": "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml",
    "Gazzetta dello Sport": "https://www.gazzetta.it/rss/calcio.xml",
    "Football Italia": "https://football-italia.net/feed/",
    "Kicker": "https://newsfeed.kicker.de/news/fussball",
    "L'Équipe": "https://dwh.lequipe.fr/api/edito/rss?path=/Football/",
    "Record": "https://www.record.pt/rss",
    # местные лиги
    "NOS (Нидерланды)": "https://feeds.nos.nl/nosvoetbal",
    "Football Oranje": "https://football-oranje.com/feed",
    "Turkish League": "https://turkishleague.net/feed",
    "Fanatik (Турция)": "https://www.fanatik.com.tr/rss/futbol",
    "Sporza (Бельгия)": "https://sporza.be/nl/categorie/voetbal.rss.xml",
    "Walfoot (Бельгия)": "https://www.walfoot.be/rss",
    "Daily Record (Шотландия)": "https://www.dailyrecord.co.uk/sport/football/?service=rss",
    "The Scotsman": "https://www.scotsman.com/sport/football/rss",
}
LOCAL_SOURCES = {"NOS (Нидерланды)", "Football Oranje", "Turkish League", "Fanatik (Турция)",
                 "Sporza (Бельгия)", "Walfoot (Бельгия)", "Daily Record (Шотландия)",
                 "The Scotsman", "BBC Sport (Шотландия)", "Record"}

# Какие бесплатные модели OpenRouter предпочитаем (по порядку)
# сначала модели, которые лучше пишут по-русски
PREFERRED_MODELS = ["deepseek", "gemma-4", "qwen3", "gpt-oss-120b", "gemma", "hy3",
                    "mistral", "llama-3.3-70b", "gpt-oss", "llama", "nemotron"]
# узкоспециальные модели, которые не умеют писать тексты
BAD_MODEL_WORDS = ["safety", "guard", "embed", "rerank", "coder", "code", "vl", "vision",
                   "ocr", "math", "lightning", "nano", "mini", "tiny", "1b", "3b", "4b", "8b"]
FALLBACK_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
]

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")

STOPWORDS = set("""the and for with from that this after over into about their will have been
says said could would more than what when where they them his her its are was were
del los las por con para una que como sus mas der die das und mit von den ist
les des pour avec dans sur une che per con gli della dos das com para uma""".split())


class Overloaded(Exception):
    """ИИ перегружен или ответил ерундой — стоит повторить позже."""


class DailyLimit(Exception):
    """Кончился дневной лимит бесплатных запросов."""


# ---------------- СОСТОЯНИЕ (что уже публиковали) ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("links", [])
    s.setdefault("src_titles", [])
    s.setdefault("recent", [])
    s.setdefault("day_posts", [])
    return s


def save_state(s):
    s["links"] = s["links"][-1000:]
    s["src_titles"] = s["src_titles"][-1000:]
    s["recent"] = s["recent"][-40:]
    s["day_posts"] = s["day_posts"][-40:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def norm(text):
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def clean_html(text):
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


# ---------------- СБОР СТАТЕЙ ----------------
def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if not t:
        return None
    return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)


def fetch_candidates(state, max_age=MAX_AGE_HOURS, skip_seen=True):
    now = dt.datetime.now(dt.timezone.utc)
    seen_links = set(state["links"]) if skip_seen else set()
    seen_titles = set(state["src_titles"]) if skip_seen else set()
    recent_kws = [keywords(t) for t in state["src_titles"][-50:]] if skip_seen else []
    items = []

    def load(pair):
        source, url = pair
        try:
            r = requests.get(url, headers=UA, timeout=10)
            return source, feedparser.parse(r.content), None
        except Exception as ex:
            return source, None, ex

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(load, FEEDS.items()))

    if all(err is not None for _, _, err in results):
        raise RuntimeError("Не загрузился ни один источник новостей — похоже, проблема с сетью")

    for source, feed, err in results:
        if err is not None:
            print(f"⚠️ {source}: не загрузился ({err})")
            continue
        count = 0
        for e in feed.entries[:30]:
            link = e.get("link")
            title = clean_html(e.get("title"))
            if not link or not title:
                continue
            if link in seen_links or norm(title) in seen_titles:
                continue
            kw = keywords(title)
            if any(len(kw & r) >= 3 for r in recent_kws):
                continue
            t = entry_time(e)
            if t and (now - t).total_seconds() > max_age * 3600:
                continue
            items.append({
                "source": source,
                "title": title,
                "summary": clean_html(e.get("summary"))[:300],
                "link": link,
                "time": t,
            })
            count += 1
        print(f"{'✅' if count else '⚪'} {source}: {count} свежих")
    add_buzz(items)
    far_past = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    items.sort(key=lambda x: (x["buzz"], x["time"] or far_past), reverse=True)
    # чтобы местные лиги не терялись среди англоязычного потока
    top = items[:MAX_CANDIDATES - 12]
    local = [i for i in items[MAX_CANDIDATES - 12:] if i["source"] in LOCAL_SOURCES][:12]
    return top + local


def keywords(title):
    return {w for w in norm(title).split() if len(w) > 3 and w not in STOPWORDS}


def add_buzz(items):
    """Грубо считаем, сколько других изданий пишут о том же (общие имена/слова)."""
    kws = [keywords(i["title"]) for i in items]
    for i, item in enumerate(items):
        sources = set()
        for j, other in enumerate(items):
            if i != j and other["source"] != item["source"] and len(kws[i] & kws[j]) >= 2:
                sources.add(other["source"])
        item["buzz"] = len(sources)


def fetch_article_text(link):
    try:
        r = requests.get(link, headers=UA, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paras if len(p) > 40)
        return text[:6000]
    except Exception as ex:
        print(f"⚠️ Не смог прочитать статью: {ex}")
        return ""


# ---------------- ИИ (OpenRouter) ----------------
def good_model(mid):
    name = mid.split("/")[-1].replace(":free", "").lower()
    parts = re.split(r"[-_.]", name)
    return not any(w in parts or (len(w) > 4 and w in name) for w in BAD_MODEL_WORDS)


def get_models():
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=20)
        free = [m["id"] for m in r.json()["data"]
                if m["id"].endswith(":free") and (m.get("context_length") or 0) >= 16000
                and good_model(m["id"])]

        def rank(mid):
            for n, key in enumerate(PREFERRED_MODELS):
                if key in mid:
                    return n
            return len(PREFERRED_MODELS)

        free.sort(key=rank)
        if free:
            return free[:6]
    except Exception as ex:
        print(f"⚠️ Не получил список моделей: {ex}")
    return FALLBACK_MODELS


def clean_answer(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.replace("**", "").strip()


def parse_choice(text):
    t = clean_answer(text)
    m = re.search(r"(\d+)\s*\|\s*(news|rumor|analysis|match)", t, re.I)
    if m:
        return {"id": int(m.group(1)), "type": m.group(2).lower()}
    if re.search(r"\bНЕТ\b|\bnull\b|\bnone\b", t, re.I):
        return {"id": None, "type": None}
    raise ValueError(f"Непонятный выбор: {t[:150]}")


def parse_post(text):
    lines = [l.strip() for l in clean_answer(text).splitlines()]
    lines = [l for l in lines if l]
    if len(lines) < 2:
        raise ValueError(f"Слишком короткий ответ: {text[:150]}")
    title = re.sub(r"^(заголовок|title)\s*:\s*", "", lines[0], flags=re.I).strip('" ')
    body = "\n\n".join(re.sub(r"^(текст|body)\s*:\s*", "", l, flags=re.I) for l in lines[1:])
    if len(title) > 120 or len(body) < 40:
        raise ValueError("Ответ не похож на пост")
    return {"title": title, "body": body}


def ask_ai(models, system, user, parser):
    """Спрашиваем ИИ. Бесплатные модели часто перегружены (ошибка 429),
    поэтому пробуем разные модели и ждём между попытками."""
    last_error = ""
    for attempt in range(3):
        # на каждой попытке ставим первой другую модель, остальные — запасные
        k = attempt % len(models)
        order = models[k:] + models[:k]
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}"},
                json={"model": order[0], "models": order[:3], "temperature": 0.6,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}]},
                timeout=60,
            )
            if r.status_code in (401, 402, 403):
                raise SystemExit(f"❌ OpenRouter отказал ({r.status_code}): {r.text[:300]}\n"
                                 "Проверь ключ и его лимит в openrouter.ai → Keys.")
            if r.status_code == 429 and "per-day" in r.text:
                raise DailyLimit("Дневной лимит бесплатных запросов OpenRouter исчерпан (обнулится в 03:00 по Москве)")
            if r.status_code != 200:
                last_error = f"{r.status_code}: {r.text[:300]}"
                print(f"⚠️ Попытка {attempt + 1}: {last_error}")
                time.sleep(10 * (attempt + 1))
                continue
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            print(f"🤖 Ответила модель {data.get('model', order[0])}")
            return parser(content)
        except (SystemExit, DailyLimit):
            raise
        except Exception as ex:
            last_error = str(ex)
            print(f"⚠️ Попытка {attempt + 1}: {ex}")
            time.sleep(5)
    raise Overloaded(f"ИИ не справился за 3 попытки. Последняя ошибка: {last_error}")


SELECT_SYSTEM = """Ты главный редактор русскоязычного Telegram-канала о европейском футболе «С ударом!».
Темы канала: АПЛ, Ла Лига, Серия А, Бундеслига, Лига 1, чемпионаты Португалии, Нидерландов,
Турции, Бельгии и Шотландии, еврокубки (ЛЧ, ЛЕ, ЛК) и международные турниры сборных.
Тебе дают список свежих статей. Поле buzz показывает, сколько других изданий пишут о похожем.
Выбери ОДНУ статью для следующего поста. Приоритеты:
1) история, которую сейчас больше всего обсуждают (высокий buzz, громкие имена и клубы);
2) итоги крупных матчей, которые только что закончились;
3) интересная аналитика.
Не выбирай истории, которые уже были в недавних постах, даже в другой формулировке.
Следи за разнообразием: если в последних 5–6 постах не было Турции, Нидерландов, Бельгии,
Шотландии или Португалии, а среди статей есть заметная история оттуда (громкий матч, скандал,
трансфер известного игрока), можно выбрать её.
Не выбирай статьи не по темам канала, лайв-трансляции, тесты, опросы, рекламу, ставки.
Ответь ОДНОЙ строкой в формате НОМЕР|ТИП, например: 12|match
Если достойного нет, ответь: НЕТ
ТИП: rumor — трансферный слух или неподтверждённая информация; match — итоги матча;
analysis — разбор/мнение/лонгрид; news — подтверждённая новость."""

WRITE_SYSTEM = """Ты автор Telegram-канала о футболе «С ударом!». Пишешь по-русски дерзко и с юмором,
как остроумный фанат, который разбирается в футболе: подколки, сарказм, яркие сравнения.
Шутишь над ситуацией, результатами и решениями, но без оскорблений игроков, болельщиков,
национальностей и без мата.
Если в статье есть интересный факт или цифра (рекорд, серия, статистика, сумма трансфера),
обязательно вставь одну такую деталь — это делает пост ценнее. Бери цифры ТОЛЬКО из статьи. Перескажи статью СВОИМИ словами, не копируй фразы оригинала.
Используй ТОЛЬКО факты из текста статьи. Ничего не выдумывай: ни счёт, ни цитаты, ни цифры.
Если это слух — прямо укажи, кто об этом сообщает, и не подавай как факт.
Длина текста: СТРОГО 3–4 предложения одним абзацем, для любого типа поста. Только самое главное —
кому интересны детали, перейдёт по ссылке. Для match обязательно укажи счёт, если он есть в тексте.
Пиши грамотно и естественно, как пишет русский спортивный журналист, а не как машинный перевод:
никаких калек с английского, у каждого предложения должна быть ясная мысль и логичная связь с предыдущим.
Имена игроков и клубов пиши так, как принято в русских спортивных СМИ (Мбаппе, «Бавария»).
Можно 1–2 уместных эмодзи. Без хэштегов, без markdown, без ссылок, без подписи канала.
Заголовок должен цеплять: живой, с интригой или лёгкой иронией, как в хорошем
спортивном паблике, а не сухая газетная строка. Хорошо работают вопросы и контраст.
Примеры хорошего стиля: «Сити» виновен. Что теперь будет с титулами? /
«Челси» потратил миллиард, чтобы проиграть «Брентфорду» /
«Бавария» снова забыла, как защищаться / Ламин Ямаль и 90 минут, которые сломали «Атлетико».
Плохо: «Последствия приговора для „Манчестер Сити“», «Итоги матча „Барселона“ — „Атлетико“».
Заголовок не должен преувеличивать или менять смысл статьи.
Формат ответа строго такой, без пояснений:
первая строка — короткий заголовок до 80 символов, начинается с одного эмодзи;
дальше — текст поста."""


EDIT_SYSTEM = """Ты строгий литературный редактор и корректор русскоязычного спортивного Telegram-канала.
Тебе дают черновик поста и текст исходной статьи. Проверь и исправь:
1) орфографию, пунктуацию, согласование слов, падежи;
2) кальки и машинный перевод — перепиши так, как сказал бы русский спортивный журналист;
3) логику: каждое предложение понятно, связано с предыдущим, нет противоречий и обрывков мысли;
4) факты: всё должно совпадать со статьёй — счёт, имена, клубы, кто что сказал. Убери то, чего в статье нет;
5) имена и клубы в привычном русском написании («Бавария», Мбаппе, Ямаль);
6) длина: 3–4 предложения одним абзацем, заголовок до 80 символов с одним эмодзи в начале.
Сохрани дерзкий тон, шутки и интересные цифры — исправляй ошибки, а не вычищай характер.
Если шутка грубая, оскорбительная или непонятная — замени её на более удачную. Новых фактов не добавляй.
Верни ТОЛЬКО исправленный пост без комментариев:
первая строка — заголовок;
дальше — текст поста."""


# ---------------- TELEGRAM ----------------
def build_post(post, kind, source, link):
    title = html.escape(post["title"].strip())
    body = html.escape(post["body"].strip())
    if kind == "rumor":
        head = f"🔄 <b>Слух</b> | <b>{title}</b>"
    else:
        head = f"<b>{title}</b>"
    src = f'🔗 Источник: <a href="{html.escape(link, quote=True)}">{html.escape(source)}</a>'
    return f"{head}\n\n{body}\n\n{src}"


def send_to_telegram(text, link):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
              "link_preview_options": {"url": link, "prefer_large_media": True}},
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram отказал: {data.get('description')}")



# ---------------- ДАЙДЖЕСТ «ИТОГИ ДНЯ» ----------------
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

DIGEST_SYSTEM = """Ты автор Telegram-канала о европейском футболе «С ударом!». Составь вечерний дайджест «Итоги дня».
Пиши по-русски, живо, с лёгкой иронией. Используй ТОЛЬКО факты из присланных материалов, ничего не выдумывай.
Формат строго такой, без вступлений и пояснений:
Главное:
• эмодзи + одна-две фразы о событии
(4–6 пунктов, самые важные и обсуждаемые истории дня)

Счета:
• «Команда» 2:1 «Команда» (турнир)
(только крупные матчи топ-лиг, еврокубков и сборных; бери счёт ТОЛЬКО если он прямо указан в материалах.
Если ни одного счёта нет — раздел «Счета» не пиши вообще)
Без хэштегов, без ссылок, без markdown."""


def parse_digest(text):
    t = clean_answer(text)
    if "Главное" not in t or t.count("•") < 3:
        raise ValueError("Дайджест не похож на дайджест")
    return t[t.index("Главное"):]


def run_digest(state):
    now = dt.datetime.now(dt.timezone.utc)
    posts = [p for p in state["day_posts"]
             if (now - dt.datetime.fromisoformat(p["time"])).total_seconds() < 24 * 3600]
    news = fetch_candidates(state, max_age=24, skip_seen=False)
    if not posts and not news:
        print("За день ничего нет, дайджест пропускаем.")
        return
    material = "Посты канала за сегодня:\n" + ("\n".join(
        f"- {p['title']}: {p['body']}" for p in posts) or "(нет)")
    material += "\n\nЗаголовки новостей за сутки (ищи здесь счета матчей):\n" + "\n".join(
        f"- ({n['source']}) {n['title']} — {n['summary'][:150]}" for n in news)
    text = ask_ai(get_models(), DIGEST_SYSTEM, material, parse_digest)

    msk = now + dt.timedelta(hours=3)
    head = f"🌙 <b>Итоги дня · {msk.day} {MONTHS[msk.month - 1]}</b>"
    body = html.escape(text)
    body = re.sub(r"^(Главное|Счета):", r"<b>\1:</b>", body, flags=re.M)
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL, "text": f"{head}\n\n{body}", "parse_mode": "HTML",
              "link_preview_options": {"is_disabled": True}},
        timeout=30,
    )
    if not r.json().get("ok"):
        raise RuntimeError(f"Telegram отказал: {r.json().get('description')}")
    print("🌙 Дайджест опубликован:\n" + text)

# ---------------- ГЛАВНОЕ ----------------
def run(state):
    if os.environ.get("MODE") == "digest":
        run_digest(state)
        return
    cands = fetch_candidates(state)
    print(f"📰 Кандидатов: {len(cands)}")
    if not cands:
        print("Нечего публиковать, выходим.")
        return

    models = get_models()
    print(f"Модели: {models}")

    lines = []
    for n, c in enumerate(cands):
        lines.append(f"[{n}] ({c['source']}, buzz={c['buzz']}) {c['title']} — {c['summary'][:200]}")
    recent = "\n".join(f"- {r}" for r in state["recent"][-30:]) or "(пока нет)"
    choice = ask_ai(models, SELECT_SYSTEM,
                    f"Недавние посты канала:\n{recent}\n\nСвежие статьи:\n" + "\n".join(lines), parse_choice)
    print(f"🎯 Выбор: {choice}")

    idx = choice.get("id")
    if idx is None or not (0 <= idx < len(cands)):
        print("ИИ решил, что сейчас публиковать нечего.")
        return
    item = cands[idx]
    kind = choice.get("type", "news")

    text = fetch_article_text(item["link"])
    if len(text) < 200:
        text = f"{item['title']}\n{item['summary']}\n{text}"

    post = ask_ai(models, WRITE_SYSTEM,
                  f"Тип поста: {kind}\nИздание: {item['source']}\n"
                  f"Заголовок: {item['title']}\n\nТекст статьи:\n{text}", parse_post)

    print(f"📝 Черновик:\n{post['title']}\n{post['body']}")
    # без проверки корректором не публикуем
    post = ask_ai(models, EDIT_SYSTEM,
                  f"Черновик:\n{post['title']}\n{post['body']}\n\n"
                  f"Исходная статья ({item['source']}):\n{text[:3500]}", parse_post)

    message = build_post(post, kind, item["source"], item["link"])
    send_to_telegram(message, item["link"])
    print("🚀 Опубликовано:\n" + message)

    state["links"].append(item["link"])
    state["src_titles"].append(norm(item["title"]))
    state["recent"].append(post["title"])
    state["day_posts"].append({"time": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "title": post["title"], "body": post["body"][:400]})
    state["last_post"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_state(state)


def fail(message):
    """Пишем причину в лог и завершаем запуск с ошибкой (красный крестик в Actions)."""
    print(f"❌ {message}")
    raise SystemExit(1)


def main():
    if not TG_TOKEN or not OR_KEY:
        fail("Нет секретов TELEGRAM_BOT_TOKEN или OPENROUTER_API_KEY")

    state = load_state()
    now = dt.datetime.now(dt.timezone.utc)
    is_retry = os.environ.get("RETRY") == "1"
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    # запасной запуск: нужен, только если основной за последний час ничего не выложил
    if is_retry and state.get("last_post"):
        ago = (now - dt.datetime.fromisoformat(state["last_post"])).total_seconds()
        if ago < 70 * 60:
            print("Основной запуск уже выложил пост, запасной не нужен.")
            return

    today = (now + dt.timedelta(hours=3)).date().isoformat()
    if state.get("limit_day") == today and not is_manual:
        print("Сегодня лимит ИИ уже кончился, ждём завтра.")
        return

    try:
        run(state)
    except DailyLimit as ex:
        state["limit_day"] = today
        save_state(state)
        fail(str(ex))
    except Overloaded as ex:
        if is_retry or is_manual:
            fail(f"Пост пропущен: {ex}")
        print(f"⏳ {ex}\nПопробую снова через 30 минут.")
    except SystemExit:
        raise
    except Exception as ex:
        fail(f"{type(ex).__name__}: {ex}")


if __name__ == "__main__":
    main()
