import calendar
import datetime
import html
import json
import os
import random
import re
import sys

import feedparser
import requests

TOKEN = os.environ["BOT2_TOKEN"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
CHANNEL = os.environ.get("CHANNEL", "@s_ydarom")

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
SLOTS = {6: "joke", 9: "history", 12: "meme", 15: "joke", 18: "situation", 21: "meme"}
KINDS = ["joke", "meme", "history", "situation"]

TOPICS = [
    "пенальти в добавленное время",
    "VAR и отмена гола",
    "трансферное окно и слухи",
    "ничья на 90-й минуте",
    "болельщик, который «всё знает лучше тренера»",
    "сборная перед важным матчем",
    "ранний матч в выходной",
    "фэнтези-футбол и капитан, который не забил",
    "тренер на пресс-конференции после поражения",
    "вратарь против нападающего",
    "стадионные пирожки и пиво",
    "сезон, который «начался хорошо»",
    "судья и жёлтая карточка",
    "игра в FIFA против друга",
]

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
def llm(prompt):
    for model in MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OR_KEY}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.9,
                },
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


def proofread(text):
    fixed = llm(
        "Исправь только явные орфографические ошибки и логические несостыковки в тексте. "
        "Регистр букв, растянутые слова, разговорный стиль, переносы строк, "
        "эмодзи и длину не меняй. Верни только текст:\n\n" + text
    )
    if fixed and 0.7 * len(text) < len(fixed) < 1.4 * len(text):
        return fixed
    return text


# ---------- Telegram ----------
def tg(method, **data):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data, timeout=30)
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
    if image:
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


def og_image(link):
    """Берёт большую картинку со страницы статьи (og:image)."""
    try:
        r = requests.get(link, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        html = r.text[:200000]
        for pat in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ):
            m = re.search(pat, html)
            if m:
                return m.group(1).replace("&amp;", "&")
    except Exception as ex:
        print("og:image error:", ex)
    return None


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


def pick_news(state, need_image=False):
    items = [i for i in fetch_news()
             if i["link"] and i["title"] and i["link"] not in state["seen"]
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


def post_meme(state):
    n = pick_news(state, need_image=True)
    if not n:
        return False
    text = llm(
        f"К картинке из новости «{n['title']}» ({n['text']}) напиши подпись одной короткой "
        "фразой (до 100 знаков) как эмоциональный болельщик: реакция и эмодзи в конце. "
        "Не описывай картинку, а реагируй на ситуацию из новости. "
        "Примеры манеры (не копируй): «Вау.», «Плачут все 💔», «ОН ВЕРНУЛСЯ 🤩»."
    )
    if not text:
        return False
    text = proofread(text)
    candidates = []
    for img in (og_image(n["link"]), n["image"]):
        if img and img not in candidates:
            candidates.append(img)
    for img in candidates:
        if send(text, image=img, require_image=True):
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


def post_situation(state):
    topic = random.choice(TOPICS)
    text = llm(
        f"Тема: {topic}\n\nНапиши короткий пост от лица фаната: эмоциональная реакция "
        "или забавное наблюдение про эту вечную футбольную ситуацию, 1–2 короткие строки. "
        "Вопросов к подписчикам не задавай. "
        "Без конкретных игроков, команд, счётов и дат."
    )
    return bool(text) and send(proofread(text))


HANDLERS = {"joke": post_joke, "meme": post_meme,
            "history": post_history, "situation": post_situation}


def main():
    kind = os.environ.get("KIND", "").strip()
    if kind not in HANDLERS:
        h = datetime.datetime.utcnow().hour
        slot = max([s for s in SLOTS if s <= h], default=21)
        kind = SLOTS.get(slot) or random.choice(KINDS)
    state = load_state()
    print("kind:", kind)
    ok = HANDLERS[kind](state)
    if not ok and kind != "situation":
        print("fallback to situation")
        ok = post_situation(state)
    save_state(state)
    print("posted:", ok)


if __name__ == "__main__":
    main()
