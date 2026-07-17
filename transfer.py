#!/usr/bin/env python3
"""Перенос лайкнутых треков из Яндекс Музыки в Spotify (Liked Songs).

Нужные переменные окружения:
  YANDEX_TOKEN          - OAuth-токен Яндекс Музыки
  SPOTIPY_CLIENT_ID     - Client ID приложения Spotify
  SPOTIPY_CLIENT_SECRET - Client Secret
  SPOTIPY_REDIRECT_URI  - например http://127.0.0.1:8888/callback

Прогресс пишется в state.json - скрипт можно прерывать и перезапускать.
Ненайденные треки попадают в unmatched.csv.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from yandex_music import Client as YandexClient

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
UNMATCHED_FILE = HERE / "unmatched.csv"
SPOTIFY_CACHE = HERE / ".spotify_token_cache"

MATCH_THRESHOLD = 0.75
LIKE_BATCH = 50
FETCH_BATCH = 100
PAUSE = 0.2  # между запросами к Spotify


class DailyCap(Exception):
    """Выбрана суточная квота приложения (429 с Retry-After в часах)."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after


def sp_call(fn, *args, **kwargs):
    """Вызов Spotify API: короткие 429 и временные сбои ретраим сами,
    суточную квоту - наверх через DailyCap."""
    transient = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                # без заголовка Retry-After считаем, что это суточная квота
                retry_after = int((e.headers or {}).get("Retry-After", "999999"))
                if retry_after > 120:
                    raise DailyCap(retry_after)
                time.sleep(retry_after + 1)
                continue
            # 5xx (типа "no healthy upstream") бывают у Spotify пачками
            if e.http_status and e.http_status >= 500 and transient < 5:
                transient += 1
                print(f"  ...{e.http_status} от API, ретрай через {10 * transient} с")
                time.sleep(10 * transient)
                continue
            raise
        except requests.exceptions.RequestException:
            # сетевая ошибка (моргнул VPN и т.п.)
            if transient >= 5:
                raise
            transient += 1
            print(f"  ...сетевая ошибка, ретрай через {10 * transient} с")
            time.sleep(10 * transient)


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)  # (feat. ...), [remastered] и т.п.
    s = re.sub(r"\b(feat|ft)\.?\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)


def translit(s: str) -> str:
    return s.lower().translate(TRANSLIT)


def artist_sim(cand_name: str, ya_artists: list[str]) -> float:
    """Похожесть с учётом транслитерации (Скриптонит vs Skryptonite)."""
    best = 0.0
    for ya in ya_artists:
        best = max(best, sim(cand_name, ya), sim(translit(cand_name), translit(ya)))
    return best


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0))


def score_candidate(cand: dict, title: str, artists: list[str], dur_ms) -> float:
    title_score = sim(cand["name"], title)
    a_score = max(
        (artist_sim(a["name"], artists) for a in cand["artists"]), default=0.0
    )
    score = 0.6 * title_score + 0.4 * a_score
    # название совпало и длительность та же (±3с) - это тот же трек,
    # даже если артист записан иначе (Элджей vs Allj)
    if dur_ms and title_score >= 0.85 and abs(cand["duration_ms"] - dur_ms) <= 3000:
        score = max(score, 0.78 + 0.2 * title_score)
    return score


def search_spotify(sp: spotipy.Spotify, title: str, artists: list[str], dur_ms):
    """Возвращает (spotify_id, score) либо (None, best_score)."""
    main_artist = artists[0] if artists else ""
    queries = [
        f'track:"{title}" artist:"{main_artist}"',
        f"{main_artist} {title}",
        title,
    ]
    best_id, best_score = None, 0.0
    for q in queries:
        res = sp_call(sp.search, q=q, type="track", limit=5)
        for cand in res["tracks"]["items"]:
            score = score_candidate(cand, title, artists, dur_ms)
            if score > best_score:
                best_id, best_score = cand["id"], score
        if best_score >= 0.95:  # достаточно хорошо, дальше не ищем
            break
    if best_score >= MATCH_THRESHOLD:
        return best_id, best_score
    return None, best_score


def fetch_existing_spotify_likes(sp: spotipy.Spotify) -> set:
    ids = set()
    page = sp_call(sp.current_user_saved_tracks, limit=50)
    while page:
        ids.update(i["track"]["id"] for i in page["items"] if i["track"])
        print(f"  уже лайкнуто в Spotify: {len(ids)}", end="\r")
        page = sp_call(sp.next, page) if page["next"] else None
    print()
    return ids


def fetch_yandex_likes(token: str):
    client = YandexClient(token).init()
    likes = client.users_likes_tracks()
    shorts = list(likes.tracks)
    shorts.reverse()  # старые лайки первыми - в Spotify сохранится порядок
    print(f"В Яндексе лайков: {len(shorts)}")
    tracks = []
    for i in range(0, len(shorts), FETCH_BATCH):
        chunk = [t.track_id for t in shorts[i : i + FETCH_BATCH]]
        tracks.extend(client.tracks(chunk))
        print(f"  метаданные: {min(i + FETCH_BATCH, len(shorts))}/{len(shorts)}", end="\r")
    print()
    return tracks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="только матчинг, без добавления лайков")
    ap.add_argument("--limit", type=int, default=0, help="обработать только N треков (для проверки)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер для OAuth (для докера)")
    ap.add_argument("--retry-misses", action="store_true", help="перепроверить ранее ненайденные треки")
    args = ap.parse_args()

    yandex_token = os.environ.get("YANDEX_TOKEN")
    if not yandex_token:
        sys.exit("Не задан YANDEX_TOKEN")

    sp = spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            scope="user-library-modify user-library-read",
            cache_path=str(SPOTIFY_CACHE),
            open_browser=not args.no_browser,
        ),
        # чистая сессия без retry-адаптера: 429 обрабатываем сами в sp_call,
        # иначе urllib3 съедает Retry-After (или спит по нему сутки)
        requests_session=requests.Session(),
    )
    me = sp.current_user()
    print(f"Spotify: залогинен как {me['display_name']} ({me['id']})")

    already_liked = fetch_existing_spotify_likes(sp)
    tracks = fetch_yandex_likes(yandex_token)
    if args.limit:
        tracks = tracks[: args.limit]

    state = load_state()
    pending = []  # [(yandex_key, spotify_id)] - сматчено, но ещё не лайкнуто
    misses = []
    done = matched = 0

    def flush_likes():
        # по одному треку на запрос: при пачке все получают одинаковый
        # timestamp и Spotify перемешивает их внутри пачки
        if args.dry_run:
            pending.clear()
            return
        while pending:
            _, tid = pending[0]
            sp_call(sp.current_user_saved_tracks_add, [tid])
            pending.pop(0)
            time.sleep(PAUSE)
        save_state(state)  # в dry-run состояние не пишем

    capped = None
    try:
        for tr in tracks:
            done += 1
            if tr is None:
                continue
            key = str(tr.track_id)
            if key in state and (state[key] != "MISS" or not args.retry_misses):
                continue  # MISS перепроверяем только по флагу, бережём квоту
            title = tr.title or ""
            artists = tr.artists_name() or []
            label = f"{', '.join(artists)} - {title}"
            if not title:
                state[key] = "MISS"
                misses.append((label, "нет метаданных"))
                continue
            # подкасты/аудиокниги из лайков не переносим
            if not artists or (tr.type and tr.type != "music"):
                state[key] = "SKIP_NOT_MUSIC"
                print(f"[{done}/{len(tracks)}] ~ {label}  (подкаст, пропущен)")
                continue

            sp_id, score = search_spotify(sp, title, artists, tr.duration_ms)
            if sp_id:
                matched += 1
                state[key] = sp_id
                if sp_id in already_liked:
                    print(f"[{done}/{len(tracks)}] = {label}  (уже в лайках)")
                else:
                    already_liked.add(sp_id)
                    pending.append((key, sp_id))
                    print(f"[{done}/{len(tracks)}] + {label}  ({score:.2f})")
                    if len(pending) >= LIKE_BATCH:
                        flush_likes()
            else:
                state[key] = "MISS"
                misses.append((label, f"лучший скор {score:.2f}"))
                print(f"[{done}/{len(tracks)}] ? {label}  НЕ НАЙДЕН")
            time.sleep(PAUSE)  # чтобы не упираться в rate limit поиска
            # периодически сохраняем прогресс (кроме ещё не лайкнутых),
            # чтобы аварийная остановка не тратила квоту на перепроверку
            if done % 25 == 0 and not args.dry_run:
                pending_keys = {k for k, _ in pending}
                snap = {k: v for k, v in state.items() if k not in pending_keys}
                STATE_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=0))

        flush_likes()
    except DailyCap as e:
        capped = e
        # сматченные, но не лайкнутые треки убираем из state,
        # чтобы следующий запуск обработал их заново
        for k, _ in pending:
            state.pop(k, None)
        if not args.dry_run:
            save_state(state)

    if misses:
        with UNMATCHED_FILE.open("w", newline="") as f:
            w = csv.writer(f)
            for row in misses:
                w.writerow(row)

    total_missed = sum(1 for v in state.values() if v == "MISS")
    if capped:
        hours = capped.retry_after / 3600
        print(
            f"\nСуточная квота Spotify исчерпана (Retry-After {capped.retry_after} с, "
            f"~{hours:.1f} ч). Прогресс сохранён: {len(state)} записей. "
            f"Перезапусти скрипт после сброса квоты - продолжит с этого места."
        )
        sys.exit(2)
    print(
        f"\nГотово. Обработано {done}, найдено в этот заход {matched}, "
        f"не найдено всего {total_missed} (см. unmatched.csv)."
    )
    if args.dry_run:
        print("Это был dry-run - лайки в Spotify НЕ добавлялись.")


if __name__ == "__main__":
    main()
