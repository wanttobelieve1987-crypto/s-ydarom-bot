import calendar
import datetime
import html
import json
import os
import random
import re
import struct
import sys

import feedparser
import requests

TOKEN = os.environ["BOT2_TOKEN"]
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY_2") or os.environ.get("GEMINI_API_KEY", "")
CHANNEL = os.environ.get("CHANNEL", "@s_ydarom")
FD_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

# Модели пробуются по очереди, пока одна не ответит.
MODELS = [m for m in [
    os.environ.get("OPENROUTER_MODEL"),
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "openrouter/free",
] if m]

FEEDS = [
    "https://www.sports.ru/rss/rubric.xml?s=208",
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://www.theguardian.com/football/rss",
]

# Время запуска (UTC час) -> тип поста
SLOTS = {6: "card", 9: "history", 12: "card", 15: "meme", 18: "result", 21: "card"}
KINDS = ["card", "meme", "history", "result"]

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

SYSTEM = (
    "Ты автор русскоязычного футбольного Telegram-канала. Формат и длину поста "
    "всегда задаёт задание, держись именно его. Общие правила: простой живой русский "
    "язык, без канцелярита, без морали и без лишних объяснений. "
    "Эмодзи ставь по формату: в срочных новостях ⚡️ или ❗️ в самом начале, в реакциях "
    "болельщика одно-два в конце фразы (💔 🥺 🔥 🐐 🤨 😭 🤩 🤦‍♂️). "
    "КАПС используй редко, в одном посте из четырёх-пяти и только в по-настоящему "
    "эмоциональный момент. У канала нет комментариев, поэтому не задавай вопросов "
    "подписчикам и не зови их что-то обсуждать. "
    "Игра слов, идиомы и ирония приветствуются, но только если они логично подходят к "
    "ситуации и понятны с первого раза: бессмысленная хитрая фраза хуже простой. "
    "Без хэштегов, без markdown, без кавычек вокруг всего текста. "
    "Подколы и ирония над ситуациями, командами и фанатами допустимы, но без оскорблений "
    "игроков и тренеров лично, без политики и без мата. "
    "Не выдумывай факты, цифры, счёт, даты и цитаты: бери только то, что дано в задании. "
    "Если речь о слухе или возможном трансфере, начни текст со слова «Слух:» "
    "(после эмодзи, если он есть). "
    "Отвечай только готовым текстом поста."
)

# Формат «лента новостей»: как в крупных спортивных новостных каналах.
WIRE_STYLE = (
    "Новостная лента. Одним абзацем, 3–5 предложений. Начни с эмодзи ⚡️ или ❗️, затем "
    "заголовок с главным фактом и двоеточием (например: «⚡️ «Арсенал» впервые за 3,5 года "
    "разгромно проиграл в АПЛ: ...»), после двоеточия подробности и контекст: цифры, "
    "рекорды, сравнение с прошлым, что это значит для команды. Тон спокойный, "
    "уверенный, как у быстрого новостного канала; ирония только через сами факты, "
    "без шуток и без эмоций фаната. Используй только факты из задания."
)

NEWS_STYLES = [
    WIRE_STYLE,
    WIRE_STYLE,
    "Срочная новость, коротко. Первая строка: эмодзи ⚡️ или ❗️ и главная суть одним "
    "предложением. Вторая строка: одна короткая деталь обычным текстом.",
    "Суть новости одной короткой строкой, а следующей строкой отдельно эмоциональная "
    "реакция болельщика (например «Вау.», «Это провал 💔», «ОН ВЕРНУЛСЯ 🤩»).",
    "Суть новости одной строкой обычным регистром и короткий сухой подкол или "
    "ироничное наблюдение следующей строкой.",
]

STATE_FILE = "bot2_state.json"


# ---------- служебное ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", [])
    return s


def save_state(state):
    state["seen"] = state["seen"][-400:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def clean(text):
    text = text.strip()
    text = re.sub(r"#\w+", "", text)
    text = text.replace("**", "").replace("__", "")
    if len(text) > 2 and text[0] in "\"«" and text[-1] in "\"»":
        text = text[1:-1]
    return re.sub(r"[ \t]+\n", "\n", text).strip()


# ---------- ИИ ----------
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

GEMINI_MODELS = [m for m in [
    os.environ.get("GEMINI_MODEL"),
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
] if m]

VISION_MODELS = [m for m in [
    os.environ.get("OPENROUTER_VISION_MODEL"),
    "google/gemma-3-27b-it:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen2.5-vl-72b-instruct:free",
] if m]


def backends(vision=False):
    """Сначала Gemini (если есть ключ), потом OpenRouter как запасной."""
    out = []
    if GEMINI_KEY:
        out += [(GEMINI_URL, GEMINI_KEY, m, True) for m in GEMINI_MODELS]
    if OR_KEY:
        out += [(OR_URL, OR_KEY, m, False) for m in (VISION_MODELS if vision else MODELS)]
    return out


def image_data_url(url):
    """Gemini не открывает ссылки на картинки сам: скачиваем и кодируем."""
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    if len(r.content) > 6_000_000:
        raise ValueError("image too big")
    ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if not ctype.startswith("image/"):
        ctype = "image/jpeg"
    return f"data:{ctype};base64," + base64.b64encode(r.content).decode()


def _chat(messages, temperature, vision=False, image_url=None):
    data_url = None
    for url, key, model, is_gemini in backends(vision):
        try:
            msgs = messages
            if vision:
                if is_gemini:
                    if data_url is None:
                        data_url = image_data_url(image_url)
                    img = data_url
                else:
                    img = image_url
                msgs = messages[:-1] + [{"role": "user", "content": [
                    {"type": "text", "text": messages[-1]["content"]},
                    {"type": "image_url", "image_url": {"url": img}},
                ]}]
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "messages": msgs, "temperature": temperature},
                timeout=90,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                if content and content.strip():
                    return clean(content)
            print("model failed:", model, r.status_code, r.text[:200])
        except Exception as ex:
            print("model error:", model, ex)
    return None


def llm(prompt, system=None):
    return _chat([
        {"role": "system", "content": system or SYSTEM},
        {"role": "user", "content": prompt},
    ], 0.9)


def llm_vision(prompt, image_url):
    """ИИ смотрит на картинку и пишет текст с учётом того, что на ней видно."""
    return _chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ], 0.8, vision=True, image_url=image_url)


def proofread(text):
    if len(text) < 150:
        return text  # короткие подписи не проверяем: экономим дневной лимит запросов
    fixed = llm(
        "Исправь только явные орфографические ошибки и логические несостыковки в тексте. "
        "Регистр букв, растянутые слова, разговорный стиль, переносы строк, "
        "эмодзи и длину не меняй. Верни только текст:\n\n" + text
    )
    if fixed and 0.7 * len(text) < len(fixed) < 1.4 * len(text):
        return fixed
    return text


# ---------- Telegram ----------
def tg(method, files=None, **data):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                      data=data, files=files, timeout=60)
    if not r.ok:
        print("telegram error:", r.text[:300])
    return r


def fmt(text):
    """Новости с ⚡️/❗️: заголовок до двоеточия выделяем жирным (как в новостных каналах)."""
    lines = text.split("\n")
    first = lines[0].strip()
    rest = [html.escape(l) for l in lines[1:]]
    if first.startswith(("⚡", "❗")):
        m = re.match(r"^(\S+)\s+(.{10,170}?:)(?=\s)(.*)$", first)
        if m:
            head = (f"{m.group(1)} <b>{html.escape(m.group(2))}</b>"
                    f"{html.escape(m.group(3))}")
        elif rest:
            head = f"<b>{html.escape(first)}</b>"
        else:
            head = html.escape(first)
        return "\n".join([head] + rest)
    return "\n".join([html.escape(first)] + rest)


def send(text, image=None, require_image=False):
    """image: dict из best_image() (загружаем файл сами, без пережатия) или ссылка."""
    if image:
        if isinstance(image, dict):
            files = {"photo": ("photo.jpg", image["data"])}
            if tg("sendPhoto", files=files, chat_id=CHANNEL,
                  caption=fmt(text)[:1024], parse_mode="HTML").ok:
                return True
            files = {"photo": ("photo.jpg", image["data"])}
            if tg("sendPhoto", files=files, chat_id=CHANNEL, caption=text[:1024]).ok:
                return True
        else:
            if tg("sendPhoto", chat_id=CHANNEL, photo=image,
                  caption=fmt(text)[:1024], parse_mode="HTML").ok:
                return True
            if tg("sendPhoto", chat_id=CHANNEL, photo=image, caption=text[:1024]).ok:
                return True
        if require_image:
            return False
    if tg("sendMessage", chat_id=CHANNEL, text=fmt(text)[:4096], parse_mode="HTML").ok:
        return True
    return tg("sendMessage", chat_id=CHANNEL, text=text[:4096]).ok


# ---------- новости ----------
def image_of(e):
    best, best_w = None, -1
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            url = m.get("url")
            if not url:
                continue
            try:
                w = int(m.get("width") or 0)
            except ValueError:
                w = 0
            if w > best_w:
                best, best_w = url.replace("/standard/240/", "/standard/976/"), w
    if best:
        return best
    for l in e.get("links", []):
        if str(l.get("type", "")).startswith("image"):
            return l.get("href")
    return None


def page_images(link):
    """Картинки со страницы статьи: og:image и twitter:image."""
    out = []
    try:
        r = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        page = r.text[:250000]
        for prop in ("og:image", "twitter:image"):
            for pat in (
                rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{prop}["\']',
            ):
                m = re.search(pat, page)
                if m:
                    u = m.group(1).replace("&amp;", "&")
                    if u.startswith("//"):
                        u = "https:" + u
                    if u not in out:
                        out.append(u)
    except Exception as ex:
        print("page images error:", ex)
    return out


def upgrade_url(u):
    """Превью BBC и похожих лент меняем на крупные версии."""
    return re.sub(r"/standard/\d+/", "/standard/976/", u)


def image_size(data):
    """Размер картинки без внешних библиотек: JPEG, PNG, WebP."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = data[21:25]
                bits = int.from_bytes(b, "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except Exception:
        pass
    return None


MIN_IMAGE_WIDTH = 700


def best_image(n):
    """Скачивает все варианты картинки к новости и берёт самую большую.
    Если лучшая меньше MIN_IMAGE_WIDTH по ширине, фото не используем (лучше без него, чем размытое)."""
    if "_img" in n:
        return n["_img"]
    cands = []
    for u in page_images(n["link"]) + [n.get("image")]:
        if u:
            u = upgrade_url(u)
            if u not in cands:
                cands.append(u)
    best = None
    for u in cands:
        try:
            r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if not r.ok or len(r.content) > 9_000_000:
                continue
            size = image_size(r.content)
            if not size:
                continue
            w, h = size
            print("image:", u[:90], w, "x", h)
            if best is None or w * h > best["w"] * best["h"]:
                best = {"url": u, "data": r.content, "w": w, "h": h}
        except Exception as ex:
            print("image error:", ex)
    if best and (best["w"] < MIN_IMAGE_WIDTH or best["w"] < best["h"] * 0.5):
        print("image too small, skipped:", best["w"], "x", best["h"])
        best = None
    n["_img"] = best
    return best


def fetch_news():
    items = []
    for url in FEEDS:
        try:
            f = feedparser.parse(url, agent="Mozilla/5.0")
        except Exception as ex:
            print("feed error:", url, ex)
            continue
        for e in f.entries[:15]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            items.append({
                "title": strip_html(e.get("title", "")),
                "text": strip_html(e.get("summary", ""))[:900],
                "link": e.get("link"),
                "image": image_of(e),
                "ts": calendar.timegm(ts) if ts else 0,
            })
    return items


def pick_news(state, need_image=False, skip=()):
    items = [i for i in fetch_news()
             if i["link"] and i["title"] and i["link"] not in state["seen"]
             and i["link"] not in skip
             and (i["image"] or not need_image)]
    items.sort(key=lambda i: i["ts"], reverse=True)
    top = items[:10]
    return random.choice(top) if top else None


# ---------- типы постов ----------
def post_joke(state):
    n = pick_news(state)
    if not n:
        return False
    text = llm(
        f"Новость: {n['title']}\nДетали: {n['text']}\n\n"
        f"Формат поста: {random.choice(NEWS_STYLES)}\n"
        "Пиши по-русски и строго в этом формате."
    )
    if not text:
        return False
    if send(proofread(text)):
        state["seen"].append(n["link"])
        return True
    return False


def extract_fact(n):
    """Из новости достаём героя и суть по-русски, чтобы подпись точно была про них."""
    ans = llm(
        f"Новость: {n['title']}. {n['text']}\n\n"
        'Верни только JSON без пояснений вида {"hero": "...", "fact": "..."}. '
        "hero: главный герой новости (игрок, тренер или команда) по-русски в именительном "
        "падеже. fact: одно короткое предложение по-русски: что именно произошло, "
        "с результатом или цифрой, если они есть в тексте новости.",
        system="Ты редактор спортивных новостей. Отвечаешь только JSON.",
    )
    try:
        d = json.loads(re.search(r"\{.*\}", ans, re.S).group(0))
        hero, fact = str(d["hero"]).strip(), str(d["fact"]).strip()
        return (hero, fact) if hero and fact else None
    except Exception:
        print("extract_fact failed:", ans)
        return None


def mentions(caption, hero):
    """В подписи должен быть назван герой новости (с учётом падежных окончаний)."""
    cap = caption.lower().replace("ё", "е")
    words = [w for w in re.findall(r"[\w-]+", hero.lower().replace("ё", "е")) if len(w) >= 3]
    if not words:
        return True
    return any(w[:max(4, len(w) - 2)] in cap for w in words)


def caption_valid(caption, hero, max_len=160):
    if len(caption) > max_len:
        return False
    if re.search(r"\b(я|мне|меня|мой|моя|моё|мои|мы|нам|наш|наша)\b", caption.lower()):
        return False  # «я потратил призовой фонд»: подпись должна быть про героя, а не про автора
    return mentions(caption, hero)


def post_meme(state):
    tried = set()
    for _ in range(2):
        n = pick_news(state, need_image=True, skip=tried)
        if not n:
            return False
        tried.add(n["link"])
        info = extract_fact(n)
        if not info:
            continue
        hero, fact = info
        pic = best_image(n)
        if not pic:
            state["seen"].append(n["link"])
            continue
        prompt = (
            f"Герой новости: {hero}\nЧто произошло: {fact}\n\n"
            "Это фото к новости. Напиши подпись-мем к фото одной строкой (до 120 знаков, "
            "эмодзи в конце). Правила:\n"
            f"1) Обязательно назови героя по имени или названию: «{hero}». Подпись должна "
            "быть понятна человеку, который не читал новость.\n"
            "2) Стиль: сухая ирония, как в футбольных юмористических каналах: коротко "
            "прокомментируй именно этот факт неожиданным сравнением, преуменьшением или "
            "преувеличением. Игра слов и идиомы приветствуются, но только если они "
            "логично связаны с фактом и фото и понятны с первого раза.\n"
            "3) Не пиши от первого лица («я», «мы», «мне»), не придумывай факты, которых "
            "нет выше, не рассуждай про призы, ставки и деньги.\n"
            "4) Если фото явно не подходит к новости по настроению или сюжету (на фото "
            "радость, а новость про поражение, или на фото другой человек, или нейтральный "
            "кадр без связи с событием), ответь одним словом: НЕТ."
        )
        for _attempt in range(2):
            text = llm_vision(prompt, pic["url"])
            if not text:
                break
            if text.strip().upper().startswith("НЕТ"):
                print("photo does not fit the news:", pic["url"])
                break
            if not caption_valid(text, hero):
                print("caption rejected:", text)
                continue
            if send(text, image=pic, require_image=True):
                state["seen"].append(n["link"])
                return True
            break
        state["seen"].append(n["link"])  # не подошло, эту новость больше не пробуем
    return False


CARD_STYLES = [
    "Формат «карточка». Первая строка: эмодзи в начале и главный факт простыми "
    "разговорными словами, как другу в чате. Вторая строка: одна короткая рофлянная "
    "реакция или ироничное сравнение на этот факт.",
    "Формат «одной строкой»: факт простыми словами и в конце шутка или подкол через "
    "запятую или тире, эмодзи в конце.",
    "Формат «две строки»: первая строка сухо и коротко что случилось, вторая строка "
    "(отдельно) неожиданная ироничная мысль на тему этого факта, без объяснений.",
]


def post_card(state):
    """Неформальная новость с фото, как в тг-каналах: факт + рофл, героя называем по имени."""
    tried, n, img = set(), None, None
    for _ in range(4):
        cand = pick_news(state, skip=tried)
        if not cand:
            break
        tried.add(cand["link"])
        b = best_image(cand)
        if b:
            n, img = cand, b
            break
        if n is None:
            n = cand  # запасной вариант: новость без фото
    if not n:
        return False
    info = extract_fact(n)
    if info:
        hero, fact = info
        base = f"Герой новости: {hero}\nЧто произошло: {fact}\nДетали: {n['text']}\n\n"
    else:
        hero = ""
        base = f"Новость: {n['title']}\nДетали: {n['text']}\n\n"
    prompt = (
        base + f"{random.choice(CARD_STYLES)}\n"
        "Правила: разговорный язык, лёгкий рофл и ирония, юмор логично вытекает из самого "
        "факта. Всего 1–3 коротких предложения, до 250 знаков. Обязательно назови героя "
        "по имени или названию, чтобы было понятно, о ком речь. Не пиши от первого лица "
        "(«я», «мы»), не задавай вопросов, не придумывай факты, цифры и счёт, которых нет "
        "выше. Если в новости слух или возможный трансфер, начни с «Слух:»."
    )
    text = None
    for _ in range(2):
        cand = llm(prompt)
        if cand and (not hero or caption_valid(cand, hero, max_len=300)):
            text = cand
            break
        print("card rejected:", cand)
    if not text:
        return False
    text = proofread(text)
    if send(text, image=img):
        state["seen"].append(n["link"])
        return True
    return False


def post_history(state):
    now = datetime.datetime.utcnow()
    url = (f"https://api.wikimedia.org/feed/v1/wikipedia/ru/onthisday/all/"
           f"{now:%m}/{now:%d}")
    try:
        data = requests.get(url, headers={"User-Agent": "s-udarom-bot/1.0"}, timeout=30).json()
    except Exception as ex:
        print("wiki error:", ex)
        return False
    events = []
    for key in ("events", "selected", "births"):
        for it in data.get(key, []):
            t = it.get("text", "")
            key_id = f"hist:{it.get('year')}:{t[:40]}"
            if "футбол" in t.lower() and it.get("year") and key_id not in state["seen"]:
                events.append((key_id, it))
    if not events:
        return False
    key_id, ev = random.choice(events)
    text = llm(
        f"Факт из истории футбола. Дата: {now.day} {MONTHS[now.month - 1]}, "
        f"{ev['year']} год. Что произошло: {ev['text']}\n\n"
        f"Напиши пост из двух строк. Первая: «В этот день в {ev['year']} году…» и суть "
        "факта одним предложением. Вторая: короткая эмоция фаната («Легенды 🥺», "
        "«Были времена 💔», «Вот это был футбол 🔥»). "
        "Ничего не добавляй от себя, только факты из текста выше."
    )
    if not text:
        return False
    if send(proofread(text)):
        state["seen"].append(key_id)
        return True
    return False


RESULT_STYLES = [
    "Срочная новость о результате. Одна строка: эмодзи ⚡️ или ❗️, затем заголовок с "
    "двоеточием, где названы обе команды и счёт, затем короткая деталь (например, "
    "сколько голов забито, разница в счёте).",
    "Короткая реакция болельщика в 1–2 строки. Обязательно назови команды и счёт, чтобы "
    "пост был понятен без контекста. Эмодзи в конце. Игра слов и ирония приветствуются, "
    "если они логично связаны с этим результатом.",
]


def recent_matches():
    """Сыгранные матчи за вчера и сегодня (football-data.org, бесплатный план)."""
    today = datetime.datetime.utcnow().date()
    yesterday = today - datetime.timedelta(days=1)
    try:
        r = requests.get(
            "https://api.football-data.org/v4/matches",
            headers={"X-Auth-Token": FD_KEY},
            params={"dateFrom": yesterday.isoformat(), "dateTo": today.isoformat()},
            timeout=30,
        )
        if r.status_code != 200:
            print("football-data error:", r.status_code, r.text[:200])
            return []
        return r.json().get("matches", [])
    except Exception as ex:
        print("football-data error:", ex)
        return []


def post_result(state):
    if not FD_KEY:
        print("no FOOTBALL_DATA_KEY")
        return False
    cands = []
    for m in recent_matches():
        if m.get("status") != "FINISHED":
            continue
        key = f"match:{m.get('id')}"
        if key in state["seen"]:
            continue
        ft = (m.get("score") or {}).get("fullTime") or {}
        hs, as_ = ft.get("home"), ft.get("away")
        if hs is None or as_ is None:
            continue
        # интересность: много голов, крупный счёт, ничья с голами
        score = hs + as_ + 2 * abs(hs - as_) + (1 if hs == as_ and hs > 0 else 0)
        cands.append((score, key, m, hs, as_))
    if not cands:
        return False
    cands.sort(key=lambda c: c[0], reverse=True)
    _, key, m, hs, as_ = random.choice(cands[:5])
    home = m["homeTeam"].get("shortName") or m["homeTeam"]["name"]
    away = m["awayTeam"].get("shortName") or m["awayTeam"]["name"]
    comp = (m.get("competition") or {}).get("name", "")
    text = llm(
        f"Матч завершён: {home} {hs}:{as_} {away} ({comp}).\n\n"
        f"Формат поста: {random.choice(RESULT_STYLES)}\n"
        "Используй только эти данные: команды, счёт, турнир. Не называй авторов голов, "
        "минуты и другие детали, которых здесь нет."
    )
    if not text:
        return False
    if send(proofread(text)):
        state["seen"].append(key)
        return True
    return False


HANDLERS = {"card": post_card, "joke": post_joke, "meme": post_meme,
            "history": post_history, "result": post_result}


def main():
    kind = os.environ.get("KIND", "").strip()
    if kind not in HANDLERS:
        h = datetime.datetime.utcnow().hour
        slot = max([s for s in SLOTS if s <= h], default=21)
        kind = SLOTS.get(slot) or random.choice(KINDS)
    state = load_state()
    print("kind:", kind)
    ok = HANDLERS[kind](state)
    if not ok:
        # запасной вариант: другой тип, но всегда со ссылкой на реальный факт
        for fb in ("card", "joke", "result"):
            if fb != kind and not ok:
                print("fallback to", fb)
                ok = HANDLERS[fb](state)
    save_state(state)
    print("posted:", ok)
    if not ok:
        print("НИЧЕГО НЕ ОПУБЛИКОВАНО: смотри строки выше (model failed, caption rejected, telegram error)")
        sys.exit(1)


if __name__ == "__main__":
    main()
