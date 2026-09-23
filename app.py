#!/usr/bin/env python3
"""Wortschatz – adaptive German vocabulary trainer (B2 -> C2).

Run:  python3 app.py        then open http://127.0.0.1:8765
No third-party packages needed. Progress is stored in german.db (SQLite).
"""
import csv
import io
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WORTSCHATZ_DB", ROOT / "german.db"))
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
PORT = int(os.environ.get("PORT", 8765))

LEVELS = ["B2", "C1", "C2"]
DIRECTIONS = ("de2en", "en2de")
MIN, DAY = 60, 86400
MASTERED_INTERVAL = 21 * DAY
DEFAULT_SETTINGS = {"levels": LEVELS, "mode": "mixed", "new_per_day": 25}

SCHEMA = """
CREATE TABLE IF NOT EXISTS words(
  id INTEGER PRIMARY KEY,
  de TEXT NOT NULL UNIQUE,
  en TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'C1',
  example TEXT NOT NULL DEFAULT '',
  example_en TEXT NOT NULL DEFAULT '',
  rank INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS progress(
  word_id INTEGER NOT NULL REFERENCES words(id) ON DELETE CASCADE,
  direction TEXT NOT NULL,
  reps INTEGER NOT NULL DEFAULT 0,
  ease REAL NOT NULL DEFAULT 2.5,
  interval REAL NOT NULL DEFAULT 0,
  due REAL NOT NULL,
  correct INTEGER NOT NULL DEFAULT 0,
  wrong INTEGER NOT NULL DEFAULT 0,
  lapses INTEGER NOT NULL DEFAULT 0,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY(word_id, direction)
);
CREATE TABLE IF NOT EXISTS log(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  word_id INTEGER NOT NULL,
  direction TEXT NOT NULL,
  quality INTEGER NOT NULL,
  answer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(de TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS progress_due ON progress(due);
CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
"""


# ---------------------------------------------------------------- database

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get_settings(con):
    s = dict(DEFAULT_SETTINGS)
    for r in con.execute("SELECT key, value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = json.loads(r["value"])
    return s


def save_settings(con, data):
    s = get_settings(con)
    if "levels" in data:
        s["levels"] = [lv for lv in data["levels"] if lv in LEVELS] or LEVELS
    if data.get("mode") in ("mixed", *DIRECTIONS):
        s["mode"] = data["mode"]
    if "new_per_day" in data:
        s["new_per_day"] = max(0, min(500, int(data["new_per_day"])))
    for k in DEFAULT_SETTINGS:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(s[k])))
    return s


def parse_rows(text):
    """Parse 'german;english;level;example;example_en;rank' lines (tab also works)."""
    rows = []
    delim = "\t" if "\t" in text and ";" not in text else ";"
    for fields in csv.reader(io.StringIO(text), delimiter=delim):
        fields = [f.strip() for f in fields]
        if len(fields) < 2 or not fields[0] or fields[0].startswith("#"):
            continue
        if fields[0].lower() in ("german", "de", "deutsch"):
            continue  # header
        level = fields[2].upper() if len(fields) > 2 and fields[2].upper() in LEVELS else "C1"
        example = fields[3] if len(fields) > 3 else ""
        example_en = fields[4] if len(fields) > 4 else ""
        rank = int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 0
        rows.append((fields[0], fields[1], level, example, example_en, rank))
    return rows


def insert_rows(con, rows, respect_tombstones=False):
    added = 0
    for de, en, level, example, example_en, rank in rows:
        if respect_tombstones and con.execute("SELECT 1 FROM tombstones WHERE de=?", (de,)).fetchone():
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO words(de, en, level, example, example_en, rank, created) VALUES (?,?,?,?,?,?,?)",
            (de, en, level, example, example_en, rank, time.time()))
        added += cur.rowcount
        if not cur.rowcount and example_en:
            # Existing word: fill in a missing example translation, never overwrite user edits.
            con.execute("UPDATE words SET example_en=? WHERE de=? AND example_en='' AND example=?",
                        (example_en, de, example))
    return added


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        cols = [r[1] for r in con.execute("PRAGMA table_info(words)")]
        if "example_en" not in cols:
            con.execute("ALTER TABLE words ADD COLUMN example_en TEXT NOT NULL DEFAULT ''")
        if "rank" not in cols:
            con.execute("ALTER TABLE words ADD COLUMN rank INTEGER NOT NULL DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS words_rank ON words(level, rank)")
        # (Re)import seed files whenever they change; existing words are kept as-is.
        seen = json.loads((con.execute("SELECT value FROM settings WHERE key='_seed_mtimes'").fetchone()
                           or ["{}"])[0])
        for f in sorted(DATA_DIR.glob("*.csv")):
            mtime = f.stat().st_mtime
            if seen.get(f.name) != mtime:
                n = insert_rows(con, parse_rows(f.read_text(encoding="utf-8")), respect_tombstones=True)
                print(f"  imported {n} new words from {f.name}")
                seen[f.name] = mtime
        con.execute("INSERT OR REPLACE INTO settings VALUES ('_seed_mtimes', ?)", (json.dumps(seen),))


# ---------------------------------------------------------------- answer checking

UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
ARTICLE_RE = re.compile(r"^(der|die|das) (.+)$")
EN_SUBS = [(r"\b(somebody|sb)\b", "someone"), (r"\b(sth)\b", "something"),
           (r"\b(your|his|her|their|my)\b", "one's"), (r"\bsomeone's\b", "one's"),
           (r"\bcolour", "color"), (r"\bfavour", "favor"), (r"ise\b", "ize")]
DE_SUBS = [(r"\bjmdm\b", "jemandem"), (r"\bjmdn\b", "jemanden"), (r"\bjmds\b", "jemandes"),
           (r"\bjmd\b", "jemand"), (r"\betw\b", "etwas")]


def alts(s):
    return [a.strip() for a in s.split("|") if a.strip()]


def norm(s, lang):
    s = unicodedata.normalize("NFC", s).lower().replace("’", "'").replace("-", " ")
    s = re.sub(r"[.,!?;:\"()\[\]…]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if lang == "en":
        for pat, rep in EN_SUBS:
            s = re.sub(pat, rep, s)
        s = re.sub(r"^(to|a|an|the) ", "", s)
    else:
        for pat, rep in DE_SUBS:
            s = re.sub(pat, rep, s)
    return s


def lev(a, b):
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def allowed_typos(s):
    n = len(s)
    return 0 if n < 4 else 1 if n < 9 else 2 if n < 20 else 3


def compare(a, e, lang):
    """Return (quality 0-5, verdict) for normalized answer a vs expected e."""
    if a == e:
        return 5, "correct"
    fa, fe = a.translate(UMLAUTS), e.translate(UMLAUTS)
    if lang == "de":
        if fa == fe:
            return 4, "umlaut"
        m = ARTICLE_RE.match(e)
        if m:
            article, noun = m.groups()
            if a == noun or fa == noun.translate(UMLAUTS):
                return 2, "article_missing"
            ma = ARTICLE_RE.match(a)
            if ma and ma.group(1) != article and lev(ma.group(2).translate(UMLAUTS), noun.translate(UMLAUTS)) <= allowed_typos(noun):
                return 1, "wrong_article"
        if e.startswith("sich ") and a == e[5:]:
            return 3, "sich_missing"
    if lev(fa, fe) <= allowed_typos(fe):
        return 3, "typo"
    return 0, "wrong"


def best_match(answer, expected_alts, lang):
    a = norm(answer, lang)
    if not a:
        return 0, "unknown", None
    best = (-1, "wrong", None)
    for exp in expected_alts:
        q, v = compare(a, norm(exp, lang), lang)
        if q > best[0]:
            best = (q, v, exp)
    return best


def word_type(w):
    de0, en0 = alts(w["de"])[0], alts(w["en"])[0]
    if ARTICLE_RE.match(de0.lower()):
        return "noun"
    n = len(de0.split())
    if n >= 3:
        return "idiom"
    if en0.lower().startswith("to "):
        return "verb" if n <= 2 else "idiom"
    return "adj / adv"


def mask(s):
    return " ".join(w[0] + "_" * (len(w) - 1) for w in s.split())


# ---------------------------------------------------------------- scheduling (SM-2 variant)

def schedule(p, q, now):
    reps, ease, interval, lapses = p["reps"], p["ease"], p["interval"], p["lapses"]
    if q < 3:
        if reps > 0:
            lapses += 1
        reps = 0
        ease = max(1.3, ease - 0.2)
        interval = 2 * MIN if q <= 1 else 6 * MIN
    else:
        reps += 1
        if reps == 1:
            interval = DAY if q >= 4 else 10 * MIN
        elif reps == 2:
            interval = 3 * DAY if q >= 4 else DAY
        else:
            interval = max(interval, DAY) * ease * {3: 0.8, 4: 1.0, 5: 1.15}[q]
        ease = max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
        if interval >= DAY:
            interval *= random.uniform(0.93, 1.07)  # spread reviews out
    return {"reps": reps, "ease": ease, "interval": interval, "lapses": lapses, "due": now + interval}


def human(sec):
    if sec < 90 * MIN:
        return f"{max(1, round(sec / MIN))} min"
    if sec < 1.5 * DAY:
        return f"{round(sec / 3600)} h"
    if sec < 60 * DAY:
        return f"{round(sec / DAY)} days"
    return f"{round(sec / (30 * DAY))} months"


def day_start(days_ago=0):
    d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return d.timestamp()


# ---------------------------------------------------------------- core actions

def filters(s):
    levels = s["levels"] or LEVELS
    dirs = DIRECTIONS if s["mode"] == "mixed" else (s["mode"],)
    return levels, dirs, ",".join("?" * len(levels)), ",".join("?" * len(dirs))


def summary(con, s):
    levels, dirs, lv, dv = filters(s)
    now = time.time()
    due = con.execute(f"""SELECT COUNT(*) FROM progress p JOIN words w ON w.id=p.word_id
        WHERE w.level IN ({lv}) AND p.direction IN ({dv}) AND p.due <= ?""", [*levels, *dirs, now]).fetchone()[0]
    new_today = con.execute("SELECT COUNT(*) FROM progress WHERE first_seen >= ?", (day_start(),)).fetchone()[0]
    t = con.execute("SELECT COUNT(*), SUM(quality >= 3) FROM log WHERE ts >= ?", (day_start(),)).fetchone()
    return {"due": due, "new_today": new_today, "new_limit": s["new_per_day"],
            "today_reviews": t[0], "today_correct": t[1] or 0}


def make_card(con, wid, direction, kind):
    w = con.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    src = w["de"] if direction == "de2en" else w["en"]
    return {"word_id": wid, "direction": direction, "prompt": " / ".join(alts(src)),
            "level": w["level"], "kind": kind, "type": word_type(w)}


def next_card(con, exclude):
    s = get_settings(con)
    levels, dirs, lv, dv = filters(s)
    now, ex = time.time(), exclude or -1
    base = f"""FROM progress p JOIN words w ON w.id=p.word_id
               WHERE w.level IN ({lv}) AND p.direction IN ({dv}) AND w.id != ?"""
    args = [*levels, *dirs, ex]

    def new_one():
        if summary(con, s)["new_today"] >= s["new_per_day"]:
            return None
        for d in random.sample(dirs, len(dirs)):
            r = con.execute(f"""SELECT w.id FROM words w WHERE w.level IN ({lv}) AND w.id != ?
                AND NOT EXISTS (SELECT 1 FROM progress p WHERE p.word_id=w.id AND p.direction=?)
                ORDER BY w.rank LIMIT 40""", [*levels, ex, d]).fetchall()
            if r:
                return make_card(con, random.choice(r)[0], d, "new")
        return None

    due = con.execute(f"SELECT p.word_id, p.direction {base} AND p.due <= ? ORDER BY p.due LIMIT 5",
                      [*args, now]).fetchall()
    if due:
        # Mix in a new word now and then so a big review pile doesn't block progress.
        if random.random() < 0.2 and (card := new_one()):
            return card
        r = random.choice(due)
        return make_card(con, r["word_id"], r["direction"], "review")
    if card := new_one():
        return card
    # Nothing due and daily new limit reached: drill the weakest cards.
    weak = con.execute(f"""SELECT p.word_id, p.direction {base}
        ORDER BY (p.wrong - 0.5 * p.correct) DESC, p.ease ASC, p.due ASC LIMIT 12""", args).fetchall()
    if weak:
        r = random.choice(weak)
        return make_card(con, r["word_id"], r["direction"], "practice")
    return None


VERDICT_TEXT = {
    "correct": "Richtig!",
    "umlaut": "Correct – but write the umlauts (ä ö ü ß).",
    "typo": "Almost – small spelling mistake.",
    "sich_missing": "Almost – it's reflexive, don't forget 'sich'.",
    "article_missing": "Include the article (der / die / das) – gender matters at C2!",
    "wrong_article": "Wrong article.",
    "wrong": "Not quite.",
    "unknown": "Here's the answer.",
    "synonym": "That's a valid synonym – but a different word is wanted. Try again!",
}


def check_answer(con, wid, direction, answer, hinted):
    w = con.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    target, lang = (w["en"], "en") if direction == "de2en" else (w["de"], "de")
    q, verdict, matched = best_match(answer, alts(target), lang)

    if q < 3 and direction == "en2de" and answer.strip():
        # Did they give a different German word that means the same thing?
        wanted = {norm(e, "en") for e in alts(w["en"])}
        for o in con.execute("SELECT de, en FROM words WHERE id != ?", (wid,)):
            if wanted & {norm(e, "en") for e in alts(o["en"])}:
                oq, _, om = best_match(answer, alts(o["de"]), "de")
                if oq >= 3:
                    return {"verdict": "synonym", "message": VERDICT_TEXT["synonym"], "synonym_of": om}

    if hinted and q > 3:
        q = 3
    return {"quality": q, "verdict": verdict, "message": VERDICT_TEXT[verdict], "matched": matched,
            "de": " / ".join(alts(w["de"])), "en": " / ".join(alts(w["en"])),
            "example": w["example"], "example_en": w["example_en"], "level": w["level"]}


def grade(con, wid, direction, q, answer):
    now = time.time()
    q = max(0, min(5, int(q)))
    p = con.execute("SELECT * FROM progress WHERE word_id=? AND direction=?", (wid, direction)).fetchone()
    if p is None:
        con.execute("INSERT INTO progress(word_id, direction, due, first_seen, last_seen) VALUES (?,?,?,?,?)",
                    (wid, direction, now, now, now))
        p = con.execute("SELECT * FROM progress WHERE word_id=? AND direction=?", (wid, direction)).fetchone()

    practice = p["due"] > now and p["reps"] > 0
    if practice and q >= 3:
        upd = {}  # extra practice on a card that isn't due: don't inflate its interval
        next_in = p["due"] - now
    else:
        upd = schedule(p, q, now)
        next_in = upd["interval"]
    upd["correct" if q >= 3 else "wrong"] = p["correct" if q >= 3 else "wrong"] + 1
    upd["last_seen"] = now
    sets = ", ".join(f"{k}=?" for k in upd)
    con.execute(f"UPDATE progress SET {sets} WHERE word_id=? AND direction=?", [*upd.values(), wid, direction])
    con.execute("INSERT INTO log(ts, word_id, direction, quality, answer) VALUES (?,?,?,?,?)",
                (now, wid, direction, q, answer))
    return {"next_in": human(next_in)}


def stats(con):
    s = get_settings(con)
    now = time.time()
    per_level = []
    for lvl in LEVELS:
        words = con.execute("SELECT COUNT(*) FROM words WHERE level=?", (lvl,)).fetchone()[0]
        r = con.execute("""SELECT COUNT(*), SUM(p.interval >= ?), SUM(p.interval < ?)
            FROM progress p JOIN words w ON w.id=p.word_id WHERE w.level=?""",
                        (MASTERED_INTERVAL, DAY, lvl)).fetchone()
        per_level.append({"level": lvl, "cards": words * 2, "seen": r[0],
                          "mastered": r[1] or 0, "learning": r[2] or 0})
    days = []
    for i in range(13, -1, -1):
        r = con.execute("SELECT COUNT(*), SUM(quality >= 3) FROM log WHERE ts >= ? AND ts < ?",
                        (day_start(i), day_start(i - 1))).fetchone()
        days.append({"date": datetime.fromtimestamp(day_start(i)).strftime("%a %d.%m."),
                     "reviews": r[0], "correct": r[1] or 0})
    streak, i = 0, 0
    if not days[-1]["reviews"]:
        i = 1  # today not practiced yet – streak still alive from yesterday
    while con.execute("SELECT 1 FROM log WHERE ts >= ? AND ts < ? LIMIT 1",
                      (day_start(i), day_start(i - 1))).fetchone():
        streak, i = streak + 1, i + 1
    hardest = [dict(r) for r in con.execute("""
        SELECT w.id, w.de, w.en, w.level, p.direction, p.correct, p.wrong, p.lapses, p.ease
        FROM progress p JOIN words w ON w.id=p.word_id WHERE p.wrong > 0
        ORDER BY p.wrong * 1.0 / (p.correct + p.wrong) DESC, p.wrong DESC LIMIT 20""")]
    total = con.execute("SELECT COUNT(*), SUM(quality >= 3) FROM log").fetchone()
    upcoming = con.execute("SELECT COUNT(*) FROM progress WHERE due > ? AND due <= ?",
                           (now, now + DAY)).fetchone()[0]
    return {"summary": summary(con, s), "per_level": per_level, "days": days, "streak": streak,
            "hardest": hardest, "all_reviews": total[0], "all_correct": total[1] or 0,
            "due_next_24h": upcoming, "settings": s}


def list_words(con, q):
    like = f"%{q}%"
    rows = con.execute("""SELECT * FROM words WHERE de LIKE ? OR en LIKE ?
                          ORDER BY de COLLATE NOCASE LIMIT 500""", (like, like)).fetchall()
    out = []
    for w in rows:
        d = dict(w)
        for p in con.execute("SELECT * FROM progress WHERE word_id=?", (w["id"],)):
            d[p["direction"]] = {"correct": p["correct"], "wrong": p["wrong"],
                                 "interval": human(p["interval"]) if p["interval"] else "-",
                                 "mastered": p["interval"] >= MASTERED_INTERVAL}
        out.append(d)
    total = con.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    return {"words": out, "total": total}


def save_word(con, data):
    de, en = data.get("de", "").strip(), data.get("en", "").strip()
    if not de or not en:
        raise ValueError("German and English are both required.")
    level = data.get("level") if data.get("level") in LEVELS else "C1"
    example = data.get("example", "").strip()
    example_en = data.get("example_en", "").strip()
    if data.get("id"):
        con.execute("UPDATE words SET de=?, en=?, level=?, example=?, example_en=? WHERE id=?",
                    (de, en, level, example, example_en, int(data["id"])))
        return {"id": int(data["id"])}
    if con.execute("SELECT 1 FROM words WHERE de=?", (de,)).fetchone():
        raise ValueError(f"'{de}' is already in the list.")
    con.execute("DELETE FROM tombstones WHERE de=?", (de,))
    cur = con.execute("INSERT INTO words(de, en, level, example, example_en, created) VALUES (?,?,?,?,?,?)",
                      (de, en, level, example, example_en, time.time()))
    return {"id": cur.lastrowid}


def delete_word(con, wid):
    w = con.execute("SELECT de FROM words WHERE id=?", (wid,)).fetchone()
    if w:
        con.execute("INSERT OR IGNORE INTO tombstones VALUES (?)", (w["de"],))
        con.execute("DELETE FROM words WHERE id=?", (wid,))
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def payload(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def handle_api(self, method):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            with db() as con:
                if method == "GET" and path == "/api/next":
                    ex = int(qs["exclude"]) if qs.get("exclude", "").isdigit() else None
                    return self.reply({"card": next_card(con, ex), "summary": summary(con, get_settings(con))})
                if method == "GET" and path == "/api/stats":
                    return self.reply(stats(con))
                if method == "GET" and path == "/api/words":
                    return self.reply(list_words(con, qs.get("q", "")))
                if method == "GET" and path == "/api/hint":
                    w = con.execute("SELECT de, en FROM words WHERE id=?", (int(qs["word_id"]),)).fetchone()
                    target = w["en"] if qs.get("direction") == "de2en" else w["de"]
                    return self.reply({"hint": mask(alts(target)[0])})
                if method == "POST":
                    d = self.payload()
                    if path == "/api/check":
                        return self.reply(check_answer(con, int(d["word_id"]), d["direction"],
                                                       d.get("answer", ""), d.get("hinted", False)))
                    if path == "/api/grade":
                        return self.reply(grade(con, int(d["word_id"]), d["direction"],
                                                d["quality"], d.get("answer", "")))
                    if path == "/api/settings":
                        return self.reply(save_settings(con, d))
                    if path == "/api/words":
                        return self.reply(save_word(con, d))
                    if path == "/api/import":
                        rows = parse_rows(d.get("text", ""))
                        return self.reply({"parsed": len(rows), "added": insert_rows(con, rows)})
                if method == "DELETE" and path.startswith("/api/words/"):
                    return self.reply(delete_word(con, int(path.rsplit("/", 1)[1])))
            self.reply({"error": "not found"}, 404)
        except ValueError as e:
            self.reply({"error": str(e)}, 400)
        except Exception as e:  # keep the server alive, show the error in the UI
            self.reply({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self.handle_api("GET")
        body = (STATIC_DIR / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.handle_api("POST")

    def do_DELETE(self):
        self.handle_api("DELETE")


def main():
    init_db()
    url = f"http://127.0.0.1:{PORT}"
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Wortschatz trainer running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTschüss!")


if __name__ == "__main__":
    main()
