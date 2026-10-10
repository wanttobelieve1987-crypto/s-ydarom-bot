"""Кросспостер: берёт свежие посты из t.me/s_ydarom и публикует тизеры
со ссылкой на канал в группу ВКонтакте.
Ключи ИИ не нужны. В Дзен посты переносит официальный бот Дзена @zen_sync_bot."""
import datetime
import html
import json
import os
import random
import re

import requests
from bs4 import BeautifulSoup

CHANNEL = os.environ.get("TG_CHANNEL", "s_ydarom")
STATE_FILE = "crosspost_state.json"

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "").strip().lstrip("-")


MAX_AGE_HOURS = 12      # берём только свежие посты
TEASER_LEN = 220        # сколько символов показывать

CTA = [
    "👉 Продолжение и все новости раньше всех — в Telegram:",
    "⚽ Полностью и без задержек — в нашем Telegram:",
    "🔥 Это и ещё десятки постов в день — в Telegram:",
    "📲 Читай целиком в Telegram-канале «С ударом!»:",
    "⚡ Новости выходят там раньше, чем здесь:",
]

SKIP_WORDS = ["реклама", "erid", "промокод", "розыгрыш"]

UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"}


# ---------- состояние ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"done": []}


def save_state(state):
    state["done"] = state["done"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- чтение канала ----------

def parse_views(s):
    s = (s or "").strip().upper().replace(",", ".")
    m = re.match(r"([\d.]+)\s*([KM]?)", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1))


def parse_channel(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post_id = msg["data-post"]                     # "s_ydarom/123"
        text_el = msg.select_one("div.tgme_widget_message_text")
        if not text_el:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = html.unescape(text_el.get_text()).strip()
        t = msg.select_one("time[datetime]")
        when = None
        if t:
            try:
                when = datetime.datetime.fromisoformat(t["datetime"])
            except ValueError:
                pass
        views = parse_views((msg.select_one(".tgme_widget_message_views")
                             or BeautifulSoup("", "html.parser")).get_text())
        has_media = bool(msg.select_one(
            ".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player"))
        posts.append({"id": post_id, "text": text, "when": when,
                      "views": views, "media": has_media})
    return posts


def fetch_posts():
    r = requests.get(f"https://t.me/s/{CHANNEL}", headers=UA, timeout=30)
    r.raise_for_status()
    return parse_channel(r.text)


def pick_post(posts, done):
    now = datetime.datetime.now(datetime.timezone.utc)
    good = []
    for p in posts:
        if p["id"] in done or len(p["text"]) < 40:
            continue
        low = p["text"].lower()
        if any(w in low for w in SKIP_WORDS):
            continue
        if p["when"] and now - p["when"] > datetime.timedelta(hours=MAX_AGE_HOURS):
            continue
        good.append(p)
    if not good:
        return None
    # самый просматриваемый, при равенстве — с картинкой
    good.sort(key=lambda p: (p["views"], p["media"]), reverse=True)
    return good[0]


# ---------- тизер ----------

def clean(text):
    text = re.sub(r"(?im)^\s*(источник|подписаться|👉\s*@?\w+).*$", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def make_teaser(post):
    text = clean(post["text"])
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    head = lines[0] if lines else ""
    body = " ".join(lines[1:])
    if len(head) > TEASER_LEN:
        head, body = head[:TEASER_LEN], ""
    room = TEASER_LEN - len(head)
    if body and room > 60:
        if len(body) <= room:
            cut = body
        else:
            cut = body[:room]
            # обрываем по концу предложения, если получится
            m = re.search(r"^(.*[.!?…])\s", cut + " ")
            cut = m.group(1) if m and len(m.group(1)) > 40 else cut.rsplit(" ", 1)[0] + "…"
        teaser = f"{head}\n\n{cut}"
    else:
        teaser = head
    link = f"https://t.me/{post['id']}"
    return f"{teaser}\n\n{random.choice(CTA)}\n{link}", link


# ---------- ВКонтакте ----------

def post_vk(message, link):
    if not (VK_TOKEN and VK_GROUP_ID):
        print("vk: нет VK_TOKEN или VK_GROUP_ID, пропускаю")
        return None
    r = requests.post("https://api.vk.com/method/wall.post", data={
        "owner_id": f"-{VK_GROUP_ID}",
        "from_group": 1,
        "message": message,
        "attachments": link,
        "access_token": VK_TOKEN,
        "v": "5.199",
    }, timeout=30).json()
    if "error" in r:
        print("vk error:", r["error"].get("error_code"), r["error"].get("error_msg"))
        return False
    print("vk ok:", r.get("response"))
    return True


# ---------- main ----------

def main():
    state = load_state()
    posts = fetch_posts()
    print("постов на странице:", len(posts))
    post = pick_post(posts, set(state["done"]))
    if not post:
        print("нет новых подходящих постов")
        save_state(state)
        return
    message, link = make_teaser(post)
    print("---\n" + message + "\n---")
    # помечаем пост сделанным, только если ВК его принял
    if post_vk(message, link) is True:
        state["done"].append(post["id"])
    save_state(state)


if __name__ == "__main__":
    main()
