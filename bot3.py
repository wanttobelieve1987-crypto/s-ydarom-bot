"""Бот 3: ультрас и околофутбол.

Где берёт материал (без ключей):
  1) публичные Telegram-каналы про фанатов (веб-версия t.me/s/...): фото, альбомы, видео + текст;
  2) Google News RSS по запросам «ультрас», «фанаты баннер» и т.п.

Чем пишет текст (по очереди, что первое сработает):
  GEMINI_API_KEY -> GROQ_API_KEY -> Pollinations (без ключа) -> OpenRouter (только если USE_OPENROUTER=1)
  -> режим без ИИ: аккуратная чистка исходного текста + перевод через Google Translate без ключа.
"""
import datetime
import difflib
import html
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse

import feedparser
import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["BOT3_TOKEN"]
CHANNEL = os.environ.get("CHANNEL", "@s_ydarom")
MODE = (os.environ.get("MODE") or "auto").strip().lower()  # auto / tg / news
STATE_FILE = "bot3_state.json"

# Каналы-источники (через запятую можно переопределить секретом/переменной BOT3_SOURCES)
SOURCES = [s.strip().lstrip("@") for s in os.environ.get(
    "BOT3_SOURCES",
    "supertifo,golosultras",
).split(",") if s.strip()]

NEWS_QUERIES = [
    "ультрас", "фанаты баннер", "фанаты перфоманс", "фанаты выезд",
    "фанаты пиротехника стадион", "Fan ID болельщики", "фанатский сектор",
]
NEWS_KEYWORDS = [
    "ультрас", "фанат", "болельщик", "перфоманс", "баннер", "пиротех", "файер", "тифо",
    "выезд", "трибун", "сектор", "fan id", "фанайди", "ультра",
]

MAX_AGE_HOURS = 48
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# --- фильтры ---------------------------------------------------------------
STOP_ADS = ["реклам", "erid", "розыгрыш", "промокод", "казино", "ставк", "букмек", "реквизит",
            "скидк", "купить", "заказать", "донат", "сбор средств", "фрибет", "подпишись на канал"]
STOP_EXTREME = ["свастик", "зиг", "14/88", "1488", "white power", "белая сила", "нацис",
                "ку-клукс", "kkk", "sieg", "hitler", "гитлер", "rahowa"]
# Драки по умолчанию не постим: риск блокировки канала. Включить: ALLOW_FIGHTS=1
STOP_FIGHTS = ["драк", "махач", "забив", "забил стрел", "fight", "мордобой", "избил", "избиен",
               "поножов", " лес ", "в лесу", "26 на 26", "x26", "фирма проиграла"]
ALLOW_FIGHTS = os.environ.get("ALLOW_FIGHTS") == "1"

CARD_RE = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b")


def bad_text(text):
    low = " " + text.lower() + " "
    if CARD_RE.search(text):
        return "card number"
    for w in STOP_ADS:
        if w in low:
            return "ad: " + w
    for w in STOP_EXTREME:
        if w in low:
            return "extreme: " + w
    if not ALLOW_FIGHTS:
        for w in STOP_FIGHTS:
            if w in low:
                return "fight: " + w.strip()
    return None


# --- состояние --------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("seen", [])
    st.setdefault("texts", [])
    return st


def save_state(st):
    st["seen"] = st["seen"][-1500:]
    st["texts"] = st["texts"][-200:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def norm(text):
    return re.sub(r"[^a-zа-яё0-9 ]", "", text.lower())[:300]


def is_duplicate(text, st):
    n = norm(text)
    if len(n) < 20:
        return False
    for old in st["texts"]:
        if difflib.SequenceMatcher(None, n, old).ratio() > 0.6:
            return True
    return False


# --- сбор из Telegram -------------------------------------------------------
BG_RE = re.compile(r"background-image:url\('([^']+)'\)")


def node_text(node):
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return node.get_text().strip()


def parse_views(s):
    s = (s or "").strip().upper().replace(",", ".")
    try:
        if s.endswith("K"):
            return float(s[:-1]) * 1000
        if s.endswith("M"):
            return float(s[:-1]) * 1_000_000
        return float(s)
    except ValueError:
        return 0


def parse_channel_html(page, channel):
    soup = BeautifulSoup(page, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = msg["data-post"]
        text = node_text(msg.select_one("div.tgme_widget_message_text"))
        media = []
        for a in msg.select("a.tgme_widget_message_photo_wrap"):
            m = BG_RE.search(a.get("style", ""))
            if m:
                media.append(("photo", m.group(1)))
        for v in msg.select("video.tgme_widget_message_video"):
            if v.get("src"):
                media.append(("video", v["src"]))
        t = msg.select_one("time[datetime]")
        try:
            when = datetime.datetime.fromisoformat(t["datetime"]) if t else None
        except ValueError:
            when = None
        views = parse_views(node_text(msg.select_one("span.tgme_widget_message_views")))
        posts.append({
            "id": "tg:" + post_id, "channel": channel, "text": text, "media": media[:10],
            "when": when, "views": views, "link": "https://t.me/" + post_id,
        })
    return posts


def fetch_channel(channel):
    try:
        r = requests.get(f"https://t.me/s/{channel}", headers=UA, timeout=25)
        r.raise_for_status()
        return parse_channel_html(r.text, channel)
    except Exception as e:
        print(f"channel {channel} failed: {e}")
        return []


def tg_candidates(st):
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for ch in SOURCES:
        posts = fetch_channel(ch)
        print(f"channel {ch}: {len(posts)} posts")
        for p in posts:
            if p["id"] in st["seen"]:
                continue
            if not p["media"] or len(p["text"]) < 15:
                continue
            if p["when"] and (now - p["when"]).total_seconds() > MAX_AGE_HOURS * 3600:
                continue
            why = bad_text(p["text"])
            if why:
                print(f"skip {p['id']}: {why}")
                st["seen"].append(p["id"])
                continue
            if is_duplicate(p["text"], st):
                print(f"skip {p['id']}: duplicate")
                st["seen"].append(p["id"])
                continue
            out.append(p)
    # популярные и свежие — выше
    out.sort(key=lambda p: p["views"], reverse=True)
    return out


# --- сбор новостей ----------------------------------------------------------
def news_candidates(st):
    now = time.time()
    out = []
    for q in NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:2d")
               + "&hl=ru&gl=RU&ceid=RU:ru")
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=25).content)
        except Exception as e:
            print(f"news '{q}' failed: {e}")
            continue
        for e in feed.entries[:15]:
            title = e.get("title", "")
            source = e.get("source", {}).get("title", "")
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            pid = "news:" + norm(title)[:80]
            if pid in st["seen"]:
                continue
            low = title.lower()
            if not any(k in low for k in NEWS_KEYWORDS):
                continue
            if bad_text(title) or is_duplicate(title, st):
                continue
            ts = e.get("published_parsed")
            if ts and now - time.mktime(ts) > MAX_AGE_HOURS * 3600:
                continue
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ").strip()
            out.append({"id": pid, "text": title + "\n\n" + summary, "title": title,
                        "source": source, "link": e.get("link", ""), "media": []})
    random.shuffle(out)
    return out


# --- ИИ без обязательных ключей ---------------------------------------------
STYLE = (
    "Ты редактор русскоязычного Telegram-канала про ультрас, фанатов и околофутбол "
    "(в духе каналов Ultras Action и Pitch Invasion). Перепиши материал своими словами.\n"
    "Формат ответа строго такой:\n"
    "1-я строка: заголовок без кавычек-ёлочек в начале и без эмодзи. Если в материале есть текст "
    "баннера или заряда, заголовок = этот текст в кавычках, переведённый на русский. Иначе — "
    "короткий фактический заголовок (кто, где, что сделали).\n"
    "Пустая строка.\n"
    "Затем 1–3 коротких абзаца: чьи фанаты, какой матч или выезд, что за баннер/перфоманс/пиро, "
    "повод и контекст. Живо, по-фанатски, можно сленг (выезд, домашка, сектор, заряд, перфоманс, "
    "пиро, баннер, выездной сектор), можно лёгкую иронию в конце.\n"
    "Правила: не выдумывай факты, имена, счёт и цифры, которых нет в материале; переводи "
    "иностранный текст на русский; без хэштегов, без ссылок, без призывов подписаться, без "
    "вопросов к читателям; не одобряй насилие и не упоминай экстремистскую символику. "
    "Всего не больше 700 символов.\n"
    "Если материал — реклама, сбор денег, политика без отношения к фанатам, или не про "
    "фанатов/стадионы вообще, ответь одним словом SKIP."
)


def _openai_style(url, key, model, prompt, timeout=60):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    r = requests.post(url, headers=headers, timeout=timeout, json={
        "model": model, "temperature": 0.7,
        "messages": [{"role": "system", "content": STYLE}, {"role": "user", "content": prompt}],
    })
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def ask_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    models = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest",
                          "gemini-2.5-flash", "gemini-2.0-flash"] if m]
    for m in models:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                params={"key": key}, timeout=60,
                json={"systemInstruction": {"parts": [{"text": STYLE}]},
                      "contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.7}})
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"gemini {m} failed: {str(e)[:150]}")
    return None


def ask_groq(prompt):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    for m in [os.environ.get("GROQ_MODEL"), "llama-3.3-70b-versatile", "openai/gpt-oss-120b"]:
        if not m:
            continue
        try:
            return _openai_style("https://api.groq.com/openai/v1/chat/completions", key, m, prompt)
        except Exception as e:
            print(f"groq {m} failed: {str(e)[:150]}")
    return None


def ask_pollinations(prompt):
    try:
        return _openai_style("https://text.pollinations.ai/openai", None, "openai", prompt, 90)
    except Exception as e:
        print(f"pollinations failed: {str(e)[:150]}")
        return None


def ask_openrouter(prompt):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key or os.environ.get("USE_OPENROUTER") != "1":
        return None
    for m in ["meta-llama/llama-3.3-70b-instruct:free", "google/gemma-3-27b-it:free"]:
        try:
            return _openai_style("https://openrouter.ai/api/v1/chat/completions", key, m, prompt)
        except Exception as e:
            print(f"openrouter {m} failed: {str(e)[:150]}")
    return None


def cyrillic_share(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0
    return sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters) / len(letters)


def ai_rewrite(material):
    prompt = "Материал:\n" + material[:3000]
    for fn in (ask_gemini, ask_groq, ask_pollinations, ask_openrouter):
        out = fn(prompt)
        if not out:
            continue
        out = out.strip().strip("`").strip()
        if out.upper().startswith("SKIP"):
            print(f"{fn.__name__}: SKIP")
            return "SKIP"
        if 40 <= len(out) <= 1100 and cyrillic_share(out) > 0.6:
            print(f"text by {fn.__name__}")
            return out
        print(f"{fn.__name__}: bad output, len={len(out)}")
    return None


# --- режим без ИИ -----------------------------------------------------------
def translate_free(text):
    """Google Translate без ключа (неофициальный адрес, может иногда не отвечать)."""
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single", timeout=20,
                         params={"client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text[:4000]})
        r.raise_for_status()
        return "".join(part[0] for part in r.json()[0] if part and part[0])
    except Exception as e:
        print(f"translate failed: {e}")
        return None


JUNK_LINE = re.compile(
    r"(подпис|подписаться|источник|перевод\s|по всем вопросам|t\.me/|https?://|^@\w+$)", re.I)


def clean_without_ai(text):
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if JUNK_LINE.search(s):
            continue
        s = re.sub(r"#\w+", "", s).strip()
        lines.append(s)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if text and cyrillic_share(text) < 0.5:
        text = translate_free(text) or ""
    return text


# --- оформление -------------------------------------------------------------
def trim(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    p = max(cut.rfind(". "), cut.rfind("!"), cut.rfind("\n"))
    return (cut[: p + 1] if p > limit * 0.5 else cut.rstrip()) + "…"


def build_caption(body, link, limit=1024):
    body = body.strip()
    parts = body.split("\n", 1)
    head = parts[0].strip().strip("*").strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    footer = f'\n\n<a href="{html.escape(link, quote=True)}">Источник</a>' if link else ""
    # длинную первую строку не делаем заголовком
    if len(head) > 160:
        head, rest = "", body
    room = limit - len(footer) - len(head) - 20
    rest = trim(rest, max(room, 100))
    out = (f"<b>{html.escape(head)}</b>" if head else "")
    if rest:
        out += ("\n\n" if out else "") + html.escape(rest)
    return out + footer


# --- отправка в Telegram ----------------------------------------------------
API = f"https://api.telegram.org/bot{TOKEN}/"


def tg(method, data=None, files=None):
    r = requests.post(API + method, data=data, files=files, timeout=180)
    js = r.json()
    if not js.get("ok"):
        print(f"telegram error {method}: {js}")
        return False
    return True


def download(url, limit_mb):
    try:
        r = requests.get(url, headers=UA, timeout=90, stream=True)
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(1 << 16):
            buf.write(chunk)
            if buf.tell() > limit_mb * 1024 * 1024:
                print("media too big, skipped")
                return None
        return buf.getvalue()
    except Exception as e:
        print(f"download failed: {e}")
        return None


def send_post(caption, media):
    items = []
    for kind, url in media:
        data = download(url, 19 if kind == "photo" else 48)
        if data:
            items.append((kind, data))
    base = {"chat_id": CHANNEL, "parse_mode": "HTML"}
    if not items:
        return tg("sendMessage", {**base, "text": caption, "disable_web_page_preview": "false"})
    if len(items) == 1:
        kind, data = items[0]
        method = "sendPhoto" if kind == "photo" else "sendVideo"
        ext = "jpg" if kind == "photo" else "mp4"
        extra = {"supports_streaming": "true"} if kind == "video" else {}
        return tg(method, {**base, "caption": caption, **extra}, {kind: (f"f.{ext}", data)})
    group, files = [], {}
    for i, (kind, data) in enumerate(items):
        name = f"m{i}"
        files[name] = (f"{name}.{'jpg' if kind == 'photo' else 'mp4'}", data)
        entry = {"type": kind, "media": f"attach://{name}"}
        if i == 0:
            entry.update({"caption": caption, "parse_mode": "HTML"})
        group.append(entry)
    return tg("sendMediaGroup", {"chat_id": CHANNEL, "media": json.dumps(group)}, files)


# --- основной цикл ----------------------------------------------------------
def try_post(p, st):
    st["seen"].append(p["id"])
    text = ai_rewrite(p["text"])
    if text == "SKIP":
        return False
    if not text:
        print("no AI available, using cleaned original text")
        if p["id"].startswith("news:"):
            text = p["title"]
            if cyrillic_share(text) < 0.5:
                text = translate_free(text) or text
        else:
            text = clean_without_ai(p["text"])
        if len(text) < 15:
            return False
    caption = build_caption(text, p["link"])
    if send_post(caption, p["media"]):
        st["texts"].append(norm(p["text"]))
        print("posted:", p["id"])
        return True
    return False


def main():
    st = load_state()
    order = []
    if MODE in ("auto", "tg"):
        order += tg_candidates(st)[:6]
    if MODE in ("auto", "news") and (MODE == "news" or not order or random.random() < 0.2):
        news = news_candidates(st)[:3]
        order = news + order if MODE == "news" or random.random() < 0.5 else order + news
    print(f"candidates: {len(order)}")
    ok = False
    for p in order:
        if try_post(p, st):
            ok = True
            break
    save_state(st)
    if not ok:
        print("НИЧЕГО НЕ ОПУБЛИКОВАНО: смотри строки выше (channel failed, skip, telegram error)")
        sys.exit(1)


if __name__ == "__main__":
    main()
