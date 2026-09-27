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
}

# Какие бесплатные модели OpenRouter предпочитаем (по порядку)
PREFERRED_MODELS = ["deepseek", "llama-3.3-70b", "gpt-oss", "qwen3", "gemma", "nemotron", "mistral"]
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
    return s


def save_state(s):
    s["links"] = s["links"][-1000:]
    s["src_titles"] = s["src_titles"][-1000:]
    s["recent"] = s["recent"][-40:]
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


def fetch_candidates(state):
    now = dt.datetime.now(dt.timezone.utc)
    seen_links = set(state["links"])
    seen_titles = set(state["src_titles"])
    recent_kws = [keywords(t) for t in state["src_titles"][-50:]]
    items = []
    for source, url in FEEDS.items():
        try:
            r = requests.get(url, headers=UA, timeout=20)
            feed = feedparser.parse(r.content)
        except Exception as ex:
            print(f"⚠️ {source}: не загрузился ({ex})")
            continue
        count = 0
        for e in feed.entries[:30]:
            link = e.get("link")
            title = clean_html(e.get("title"))
            if not link or not title:
                continue
            if link in seen_links or norm(title) in seen_titles:
                continue
            # та же история у другого издания, о которой уже писали
            kw = keywords(title)
            if any(len(kw & r) >= 3 for r in recent_kws):
                continue
            t = entry_time(e)
            if t and (now - t).total_seconds() > MAX_AGE_HOURS * 3600:
                continue
            items.append({
                "source": source,
                "title": title,
                "summary": clean_html(e.get("summary"))[:300],
                "link": link,
                "time": t,
            })
            count += 1
        print(f"✅ {source}: {count} свежих")
    add_buzz(items)
    far_past = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    items.sort(key=lambda x: (x["buzz"], x["time"] or far_past), reverse=True)
    return items[:MAX_CANDIDATES]


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
        r = requests.get(link, headers=UA, timeout=20)
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
def get_models():
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=20)
        free = [m["id"] for m in r.json()["data"]
                if m["id"].endswith(":free") and (m.get("context_length") or 0) >= 16000]

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


def parse_json(text):
    text = re.sub(r"```(json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("ИИ ответил не в формате JSON")
    return json.loads(text[start:end + 1])


def ask_ai(models, system, user):
    """Спрашиваем ИИ. Бесплатные модели часто перегружены (ошибка 429),
    поэтому пробуем разные модели и ждём между попытками."""
    last_error = ""
    for attempt in range(4):
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
                timeout=120,
            )
            if r.status_code in (401, 402, 403):
                raise SystemExit(f"❌ OpenRouter отказал ({r.status_code}): {r.text[:300]}\n"
                                 "Проверь ключ и его лимит в openrouter.ai → Keys.")
            if r.status_code != 200:
                last_error = f"{r.status_code}: {r.text[:300]}"
                print(f"⚠️ Попытка {attempt + 1}: {last_error}")
                time.sleep(20 * (attempt + 1))
                continue
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            print(f"🤖 Ответила модель {data.get('model', order[0])}")
            return parse_json(content)
        except SystemExit:
            raise
        except Exception as ex:
            last_error = str(ex)
            print(f"⚠️ Попытка {attempt + 1}: {ex}")
            time.sleep(10)
    raise RuntimeError(f"Ни одна модель не ответила. Последняя ошибка: {last_error}")


SELECT_SYSTEM = """Ты главный редактор русскоязычного Telegram-канала о европейском футболе «С ударом!».
Темы канала: АПЛ, Ла Лига, Серия А, Бундеслига, Лига 1, чемпионаты Португалии, Нидерландов,
Турции, Бельгии и Шотландии, еврокубки (ЛЧ, ЛЕ, ЛК) и международные турниры сборных.
Тебе дают список свежих статей. Поле buzz показывает, сколько других изданий пишут о похожем.
Выбери ОДНУ статью для следующего поста. Приоритеты:
1) история, которую сейчас больше всего обсуждают (высокий buzz, громкие имена и клубы);
2) итоги крупных матчей, которые только что закончились;
3) интересная аналитика.
Не выбирай истории, которые уже были в недавних постах, даже в другой формулировке.
Не выбирай статьи не по темам канала, лайв-трансляции, тесты, опросы, рекламу, ставки.
Если достойного нет, верни id null.
Ответь ТОЛЬКО JSON: {"id": номер или null, "type": "news" | "rumor" | "analysis" | "match", "reason": "коротко почему"}
type: rumor — трансферный слух или неподтверждённая информация; match — итоги матча;
analysis — разбор/мнение/лонгрид; news — подтверждённая новость."""

WRITE_SYSTEM = """Ты автор Telegram-канала о футболе «С ударом!». Пишешь по-русски живо, с лёгкой иронией,
но без кринжа и без оскорблений. Перескажи статью СВОИМИ словами, не копируй фразы оригинала.
Используй ТОЛЬКО факты из текста статьи. Ничего не выдумывай: ни счёт, ни цитаты, ни цифры.
Если это слух — прямо укажи, кто об этом сообщает, и не подавай как факт.
Длина: news и rumor — 2–3 предложения; analysis и match — до двух коротких абзацев.
Для match обязательно укажи счёт, если он есть в тексте.
Имена игроков и клубов пиши так, как принято в русских спортивных СМИ (Мбаппе, «Бавария»).
Можно 1–2 уместных эмодзи. Без хэштегов, без markdown, без ссылок, без подписи канала.
Заголовок короткий, до 80 символов, начинается с одного эмодзи.
Ответь ТОЛЬКО JSON: {"title": "заголовок", "body": "текст поста"}"""


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


# ---------------- ГЛАВНОЕ ----------------
def main():
    if not TG_TOKEN or not OR_KEY:
        raise SystemExit("❌ Нет секретов TELEGRAM_BOT_TOKEN или OPENROUTER_API_KEY")

    state = load_state()
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
                    f"Недавние посты канала:\n{recent}\n\nСвежие статьи:\n" + "\n".join(lines))
    print(f"🎯 Выбор: {choice}")

    idx = choice.get("id")
    if idx is None or not isinstance(idx, int) or not (0 <= idx < len(cands)):
        print("ИИ решил, что сейчас публиковать нечего.")
        return
    item = cands[idx]
    kind = choice.get("type", "news")

    text = fetch_article_text(item["link"])
    if len(text) < 200:
        text = f"{item['title']}\n{item['summary']}\n{text}"

    post = ask_ai(models, WRITE_SYSTEM,
                  f"Тип поста: {kind}\nИздание: {item['source']}\n"
                  f"Заголовок: {item['title']}\n\nТекст статьи:\n{text}")

    message = build_post(post, kind, item["source"], item["link"])
    send_to_telegram(message, item["link"])
    print("🚀 Опубликовано:\n" + message)

    state["links"].append(item["link"])
    state["src_titles"].append(norm(item["title"]))
    state["recent"].append(post["title"])
    save_state(state)


if __name__ == "__main__":
    main()
