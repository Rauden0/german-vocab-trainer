#!/usr/bin/env python3
"""Build data/words_wiktionary.csv from open data.

Inputs (download once):
  de_50k.txt       https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/de/de_50k.txt
  kaikki_de.jsonl  https://kaikki.org/dictionary/German/kaikki.org-dictionary-German.jsonl  (~1 GB)
  tatoeba dir      containing deu_sentences.tsv, eng_sentences.tsv, links.csv from
                   https://downloads.tatoeba.org/exports/ (per_language/deu, per_language/eng, links.tar.bz2)

Usage: python3 tools/build_wordlist.py de_50k.txt kaikki_de.jsonl TATOEBA_DIR
Dictionary data: Wiktionary (CC BY-SA), via kaikki.org. Frequencies: OpenSubtitles via
hermitdave/FrequencyWords. Example sentences: Wiktionary and Tatoeba (CC BY).
"""
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "words_wiktionary.csv"

SKIP_TOP = 1500            # most frequent lemmas: A1–B1, assumed known
LEVEL_BOUNDS = [(5000, "B2"), (11000, "C1"), (20000, "C2")]  # frequency rank -> level; rarer is dropped
MIN_COUNT = 20             # ignore lemmas rarer than this in the subtitle corpus
POS_OK = {"noun", "verb", "adj", "adv"}
BAD_TAGS = {"obsolete", "archaic", "dated", "rare", "dialectal", "historical", "proscribed",
            "misspelling", "nonstandard", "abbreviation", "initialism", "form-of", "alt-of"}
FORM_SKIP_TAGS = {"auxiliary", "table-tags", "inflection-template", "class"}  # e.g. 'haben' listed as a form
NOUN_OTHER_FORMS = {"masculine", "feminine", "neuter", "diminutive"}  # Erbin lists Erbe etc.
ARTICLE = {"m": "der", "f": "die", "n": "das"}
GENDER_TAGS = {"masculine": "m", "feminine": "f", "neuter": "n"}
BAD_GLOSS = re.compile(r"^(alternative|obsolete|archaic|synonym|plural|diminutive|abbreviation|"
                       r"initialism|nominalization|superlative|comparative|feminine|masculine|"
                       r"short for|clipping|misspelling|eye dialect|dated form|agent noun|"
                       r"inflection|participle|gerund|(present|past) participle)", re.I)


def split_top(s):
    """Split a gloss on commas/semicolons that are not inside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch in ",;" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def clean_alt(a, is_verb):
    a = re.sub(r"\([^)]*\)", "", a)
    a = re.sub(r"\s+", " ", a).strip(" .:")
    if not a or len(a) > 40 or "=" in a or "|" in a or ";" in a:
        return None
    if is_verb and not a.startswith("to "):
        a = "to " + a
    return a


def gloss_alts(gloss, is_verb):
    """Turn 'to carry, to bear (something)' into ['to carry', 'to bear'].
    Long descriptive glosses ('a small boat, used on inland waters') keep only a short head."""
    out = []
    for i, part in enumerate(split_top(gloss)):
        words = len(re.sub(r"\([^)]*\)", "", part).split())
        if words > (5 if is_verb else 4):
            break  # descriptive text from here on
        a = clean_alt(part, is_verb)
        if a:
            out.append(a)
    return out


def gender_of(entry):
    for h in entry.get("head_templates", []):
        if h.get("name") == "de-noun":
            spec = str(h.get("args", {}).get("1", ""))
            g = spec.split(",")[0].split(":")
            gs = [x for x in g if x in ARTICLE]
            if gs:
                return gs
    for s in entry.get("senses", []):
        gs = [GENDER_TAGS[t] for t in s.get("tags", []) if t in GENDER_TAGS]
        if gs:
            return gs
    return []


def good_senses(entry):
    out = []
    for s in entry.get("senses", []):
        tags = set(s.get("tags", []))
        if tags & BAD_TAGS or "form_of" in s or "alt_of" in s:
            continue
        glosses = s.get("glosses") or []
        if not glosses or BAD_GLOSS.match(glosses[-1]):
            continue
        out.append(s)
    return out


def pick_example(senses):
    for s in senses:
        for ex in s.get("examples") or []:
            text, en = ex.get("text", "").strip(), (ex.get("english") or ex.get("translation") or "").strip()
            if ex.get("ref") or not en or "\n" in text or "\n" in en or "please add" in en.lower():
                continue
            if len(text.split()) >= 4 and not re.search(r"\d{4}", text) and 15 <= len(text) <= 140 and len(en) <= 160 and ";" not in text + en:
                return text, en
    return "", ""


def load_sentences(tatoeba_dir):
    """German->English sentence pairs from Tatoeba, indexed by lower-cased token."""
    d = Path(tatoeba_dir)
    de = {}
    for line in open(d / "deu_sentences.tsv", encoding="utf-8"):
        sid, _, text = line.rstrip("\n").split("\t", 2)
        n = len(text.split())
        if 4 <= n <= 14 and ";" not in text:
            de[sid] = text
    en = {}
    for line in open(d / "eng_sentences.tsv", encoding="utf-8"):
        sid, _, text = line.rstrip("\n").split("\t", 2)
        en[sid] = text
    pairs = {}
    for line in open(d / "links.csv", encoding="utf-8"):
        a, b = line.split()
        if a in de and b in en and a not in pairs and ";" not in en[b]:
            pairs[a] = (de[a], en[b])
    index = {}
    for de_text, en_text in pairs.values():
        for i, tok in enumerate(re.findall(r"[\wäöüÄÖÜß]+", de_text)):
            index.setdefault(tok.lower(), []).append((de_text, en_text, tok, i))
    print(f"{len(pairs)} German-English sentence pairs loaded")
    return index


def tatoeba_example(index, forms, is_noun, used):
    best, best_score = None, None
    for f in forms:
        for de_text, en_text, tok, pos in index.get(f, ()):
            # nouns are capitalised; other words only match lower-case (except sentence-initial)
            if is_noun != tok[0].isupper() and not (not is_noun and pos == 0):
                continue
            score = abs(len(de_text.split()) - 8) + (5 if de_text in used else 0)
            if best_score is None or score < best_score:
                best, best_score = (de_text, en_text), score
    return best


def main(freq_path, kaikki_path, tatoeba_dir=None):
    freq = {}
    for line in open(freq_path, encoding="utf-8"):
        w, _, c = line.strip().rpartition(" ")
        if w and c.isdigit():
            freq[w] = freq.get(w, 0) + int(c)

    lemmas = {}   # de-string -> dict
    with open(kaikki_path, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            pos, word = e.get("pos"), e.get("word", "")
            if pos not in POS_OK or not word or len(word) < 3 or not word.isalpha():
                continue
            senses = good_senses(e)
            if not senses:
                continue
            # frequency = lemma + all its inflected forms (lower-cased, as in the subtitle list)
            forms = {word.lower()} | {x["form"].lower() for x in e.get("forms", [])
                                      if isinstance(x.get("form"), str) and " " not in x["form"]
                                      and not set(x.get("tags", [])) & FORM_SKIP_TAGS
                                      and not (pos == "noun" and set(x.get("tags", [])) & NOUN_OTHER_FORMS)}
            count = max(freq.get(x, 0) for x in forms)
            if count < MIN_COUNT:
                continue
            is_verb = pos == "verb"
            if pos == "noun":
                gs = gender_of(e)
                if not gs or not word[0].isupper():
                    continue
                de = "|".join(f"{ARTICLE[g]} {word}" for g in dict.fromkeys(gs))
            else:
                if word[0].isupper():
                    continue
                de = word
            alts = []
            for s in senses[:4]:
                for a in gloss_alts(s["glosses"][0], is_verb):
                    if a.lower() not in (x.lower() for x in alts):
                        alts.append(a)
            if not alts or all(a.lower() in (word.lower(), "to " + word.lower()) for a in alts):
                continue  # no translation, or trivial cognate like Podium -> podium
            item = lemmas.setdefault(de, {"de": de, "alts": [], "count": 0, "ex": ("", ""), "pos": pos,
                                          "forms": set()})
            item["forms"] |= forms
            item["count"] = max(item["count"], count)
            item["alts"] += [a for a in alts if a.lower() not in (x.lower() for x in item["alts"])]
            if not item["ex"][0]:
                item["ex"] = pick_example(senses)

    # Feminine forms (die Lehrerin) add little once the base word (der Lehrer) is there.
    bases = {d.split("|")[0].split(" ", 1)[-1] for d, it in lemmas.items() if it["pos"] == "noun"}
    for d in [d for d, it in lemmas.items() if it["pos"] == "noun" and d.startswith("die ")
              and d.endswith("in") and d[4:-2] in bases]:
        del lemmas[d]
    ranked = sorted(lemmas.values(), key=lambda x: -x["count"])
    index = load_sentences(tatoeba_dir) if tatoeba_dir else {}
    used = set()
    rows, levels = [], {}
    for i, item in enumerate(ranked):
        if i < SKIP_TOP:
            continue
        if i >= LEVEL_BOUNDS[-1][0]:
            break
        level = next(lv for bound, lv in LEVEL_BOUNDS if i < bound)
        if not item["ex"][0] and index:
            ex = tatoeba_example(index, item["forms"], item["pos"] == "noun", used)
            if ex:
                item["ex"] = ex
                used.add(ex[0])
        levels[level] = levels.get(level, 0) + 1
        rows.append([item["de"], "|".join(item["alts"][:6]), level, item["ex"][0], item["ex"][1], i])

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("german;english;level;example;example_en;rank\n")
        csv.writer(f, delimiter=";", lineterminator="\n").writerows(rows)
    with_ex = sum(1 for r in rows if r[3])
    print(f"{len(ranked)} lemmas found, wrote {len(rows)} words to {OUT}")
    print(f"levels: {levels}; with example sentence: {with_ex}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
