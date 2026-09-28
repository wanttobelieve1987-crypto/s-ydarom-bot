"""
Счета матчей для канала «С ударом!».
Раз в 30 минут проверяет завершившиеся матчи ЛЧ и топ-5 лиг (football-data.org)
и публикует новые результаты одним постом. Разгромы и поражения грандов — с 🔥.
"""
import os
import re
import html
import json
import datetime as dt

import requests

from bot import CHANNEL, TG_TOKEN, OR_KEY, get_models, ask_ai, clean_answer

FD_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
STATE_FILE = "scores.json"

# порядок лиг в посте
COMPETITIONS = {
    "CL": "⭐ Лига чемпионов",
    "PL": "🏴\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F АПЛ",
    "PD": "🇪🇸 Ла Лига",
    "SA": "🇮🇹 Серия А",
    "BL1": "🇩🇪 Бундеслига",
    "FL1": "🇫🇷 Лига 1",
}

# гранды: поражение от «не гранда» = сенсация (сверяем по английскому названию из API)
GRANDS = ["Real Madrid", "Barcelona", "Atlético", "Atletico", "Manchester City", "Man City",
          "Arsenal", "Liverpool", "Chelsea", "Manchester United", "Man United", "Tottenham",
          "Bayern", "Dortmund", "Leverkusen", "Inter", "Milan", "Juventus", "Napoli",
          "Paris Saint-Germain", "PSG"]

# если матч начался вместе с этим, но ещё идёт — подождём, чтобы выложить всё одним постом
SAME_SLOT_MINUTES = 20
MAX_WAIT_HOURS = 3


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("posted", [])
    s.setdefault("names", {})
    s.setdefault("initialized", False)
    return s


def save_state(s):
    s["posted"] = s["posted"][-600:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def fetch_matches():
    today = dt.datetime.now(dt.timezone.utc).date()
    r = requests.get(
        "https://api.football-data.org/v4/matches",
        headers={"X-Auth-Token": FD_KEY},
        params={"competitions": ",".join(COMPETITIONS),
                "dateFrom": str(today - dt.timedelta(days=1)),
                "dateTo": str(today + dt.timedelta(days=1))},
        timeout=30,
    )
    if r.status_code != 200:
        raise SystemExit(f"❌ football-data.org ответил {r.status_code}: {r.text[:300]}")
    return r.json().get("matches", [])


def kickoff(m):
    return dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))


def is_grand(team):
    name = f"{team.get('name', '')} {team.get('shortName', '')}"
    return any(g.lower() in name.lower() for g in GRANDS)


def is_sensation(m, hg, ag):
    if abs(hg - ag) >= 4 or hg + ag >= 7:
        return True
    home, away = m["homeTeam"], m["awayTeam"]
    if hg < ag and is_grand(home) and not is_grand(away):
        return True
    if ag < hg and is_grand(away) and not is_grand(home):
        return True
    return False


def translate_names(state, teams):
    """Переводим новые названия клубов один раз и запоминаем навсегда."""
    missing = {str(t["id"]): (t.get("shortName") or t["name"]) for t in teams
               if str(t["id"]) not in state["names"]}
    if not missing:
        return
    if not OR_KEY:
        return
    lines = "\n".join(f"{i}|{n}" for i, n in missing.items())
    system = ("Переведи названия футбольных клубов на русский так, как их пишут русские спортивные СМИ "
              "(Манчестер Сити, Бавария, Атлетико, ПСЖ, Интер). Без кавычек и без слов «ФК». "
              "Ответь строками в формате ID|Название, по одной на клуб, без пояснений.")

    def parse(text):
        result = {}
        for line in clean_answer(text).splitlines():
            m = re.match(r"\s*(\d+)\s*\|\s*(.+?)\s*$", line)
            if m and m.group(1) in missing:
                result[m.group(1)] = m.group(2).strip('«»"')
        if len(result) < len(missing) / 2:
            raise ValueError("Перевёл слишком мало названий")
        return result

    try:
        state["names"].update(ask_ai(get_models(), system, lines, parse))
    except SystemExit:
        pass
    except Exception as ex:
        print(f"⚠️ Не смог перевести названия ({ex}), оставлю английские")


def team_name(state, team):
    return state["names"].get(str(team["id"])) or team.get("shortName") or team["name"]


def score_line(state, m):
    ft = m["score"]["fullTime"]
    hg, ag = ft["home"], ft["away"]
    line = f"{team_name(state, m['homeTeam'])} {hg}:{ag} {team_name(state, m['awayTeam'])}"
    pens = m["score"].get("penalties") or {}
    if m["score"].get("duration") == "PENALTY_SHOOTOUT" and pens.get("home") is not None:
        line += f" (пен. {pens['home']}:{pens['away']})"
    return ("🔥 " if is_sensation(m, hg, ag) else "") + html.escape(line)


def main():
    if not FD_KEY or not TG_TOKEN:
        raise SystemExit("❌ Нет секретов FOOTBALL_DATA_KEY или TELEGRAM_BOT_TOKEN")
    state = load_state()
    matches = fetch_matches()
    now = dt.datetime.now(dt.timezone.utc)

    finished = [m for m in matches if m["status"] == "FINISHED"
                and m["score"]["fullTime"]["home"] is not None]
    new = [m for m in finished if m["id"] not in state["posted"]]

    # первый запуск: всё, что уже сыграно, считаем опубликованным, чтобы не вывалить старьё
    if not state["initialized"]:
        state["posted"] += [m["id"] for m in finished]
        state["initialized"] = True
        save_state(state)
        print(f"Первый запуск: запомнил {len(finished)} сыгранных матчей, публикую со следующего раза.")
        return

    if not new:
        print("Новых результатов нет.")
        return

    live = [m for m in matches if m["status"] in ("IN_PLAY", "PAUSED")]
    oldest_new = min(kickoff(m) for m in new)
    same_slot_live = [m for m in live if any(
        abs((kickoff(m) - kickoff(n)).total_seconds()) <= SAME_SLOT_MINUTES * 60 for n in new)]
    if same_slot_live and (now - oldest_new).total_seconds() < MAX_WAIT_HOURS * 3600:
        print(f"Ещё идут {len(same_slot_live)} матч(а) того же тура, ждём, чтобы выложить всё вместе.")
        return

    teams = [t for m in new for t in (m["homeTeam"], m["awayTeam"])]
    translate_names(state, teams)

    blocks = []
    for code, title in COMPETITIONS.items():
        group = sorted([m for m in new if m["competition"]["code"] == code], key=kickoff)
        if group:
            blocks.append(f"<b>{title}</b>\n" + "\n".join(score_line(state, m) for m in group))
    text = "⏱ <b>Результаты</b>\n\n" + "\n\n".join(blocks)

    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": CHANNEL, "text": text, "parse_mode": "HTML",
                            "link_preview_options": {"is_disabled": True}}, timeout=30)
    if not r.json().get("ok"):
        raise RuntimeError(f"Telegram отказал: {r.json().get('description')}")
    print("🚀 Опубликовано:\n" + text)

    state["posted"] += [m["id"] for m in new]
    save_state(state)


if __name__ == "__main__":
    main()
