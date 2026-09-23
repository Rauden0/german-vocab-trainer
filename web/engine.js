/* Wortschatz – in-browser engine: a port of app.py so the trainer runs on a phone with no server.
 * Answers the same /api/... requests as the Python server; progress lives in IndexedDB and can be
 * exported/imported as progress.json (same format as the desktop app). */
(() => {
  const LEVELS = ["B2", "C1", "C2"], DIRECTIONS = ["de2en", "en2de"];
  const MIN = 60, DAY = 86400, MASTERED = 21 * DAY;
  const DEFAULT_SETTINGS = {levels: LEVELS, mode: "mixed", new_per_day: 25};
  const PROGRESS_FIELDS = ["reps", "ease", "interval", "due", "correct", "wrong", "lapses", "first_seen", "last_seen"];
  const WORD_FIELDS = ["de", "en", "level", "example", "example_en"];

  let base = [];                // words.json
  let words = [], byDe = new Map(), byRank = [], enCache = null;
  let state = {settings: {...DEFAULT_SETTINGS}, progress: {}, log: [], custom: {}, deleted: []};

  const key = (de, dir) => de + "\t" + dir;
  const now = () => Math.round(Date.now()) / 1000;   // 3 decimals, like app.py
  const pick = a => a[Math.floor(Math.random() * a.length)];
  const shuffle = a => a.map(x => [Math.random(), x]).sort((p, q) => p[0] - q[0]).map(x => x[1]);
  const alts = s => s.split("|").map(a => a.trim()).filter(Boolean);
  const fail = msg => { throw new Error(msg); };

  // ---------------------------------------------------------------- storage (IndexedDB)
  const idb = new Promise((resolve, reject) => {
    const req = indexedDB.open("wortschatz", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("kv");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  async function idbGet(k) {
    const db = await idb;
    return new Promise((res, rej) => {
      const r = db.transaction("kv").objectStore("kv").get(k);
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
  }
  async function persist() {
    const db = await idb;
    return new Promise((res, rej) => {
      const tx = db.transaction("kv", "readwrite");
      tx.objectStore("kv").put(state, "state");
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }

  function rebuild() {
    const del = new Set(state.deleted), seen = new Set(), list = [];
    for (const b of base) {
      if (del.has(b.de) || seen.has(b.de)) continue;
      seen.add(b.de);
      list.push({...b, ...(state.custom[b.de] || {})});
    }
    for (const [de, c] of Object.entries(state.custom))
      if (!seen.has(de) && !del.has(de)) list.push({rank: 0, example: "", example_en: "", ...c});
    words = list.map((w, i) => ({...w, id: i}));
    byDe = new Map(words.map(w => [w.de, w]));
    byRank = [...words].sort((a, b) => a.rank - b.rank);
    enCache = null;
  }

  const ready = (async () => {
    const data = await (await fetch("words.json")).json();
    base = data.words.map(([de, en, level, example, example_en, rank]) => ({de, en, level, example, example_en, rank}));
    const saved = await idbGet("state").catch(() => null);
    if (saved) state = {...state, ...saved, settings: {...DEFAULT_SETTINGS, ...saved.settings}};
    rebuild();
  })();

  // ---------------------------------------------------------------- answer checking
  const UML = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"};
  const fold = s => s.replace(/[äöüß]/g, c => UML[c]);
  const word = w => new RegExp(`(?<![\\p{L}\\p{N}_])(?:${w})(?![\\p{L}\\p{N}_])`, "gu");
  const EN_SUBS = [[word("somebody|sb"), "someone"], [word("sth"), "something"],
                   [word("your|his|her|their|my"), "one's"], [word("someone's"), "one's"],
                   [/(?<![\p{L}\p{N}_])colour/gu, "color"], [/(?<![\p{L}\p{N}_])favour/gu, "favor"],
                   [/ise(?![\p{L}\p{N}_])/gu, "ize"]];
  const DE_SUBS = [[word("jmdm"), "jemandem"], [word("jmdn"), "jemanden"], [word("jmds"), "jemandes"],
                   [word("jmd"), "jemand"], [word("etw"), "etwas"]];
  const ARTICLE_RE = /^(der|die|das) (.+)$/;

  function norm(s, lang) {
    s = s.normalize("NFC").toLowerCase().replace(/’/g, "'").replace(/-/g, " ");
    s = s.replace(/[.,!?;:"()\[\]…]/g, " ").replace(/\s+/g, " ").trim();
    if (lang === "en") {
      for (const [re, rep] of EN_SUBS) s = s.replace(re, rep);
      s = s.replace(/^(to|a|an|the) /, "");
    } else {
      for (const [re, rep] of DE_SUBS) s = s.replace(re, rep);
    }
    return s;
  }

  function lev(a, b) {
    if (a.length < b.length) [a, b] = [b, a];
    let prev = Array.from({length: b.length + 1}, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      for (let j = 1; j <= b.length; j++)
        cur.push(Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] !== b[j - 1])));
      prev = cur;
    }
    return prev[b.length];
  }
  const allowedTypos = s => s.length < 4 ? 0 : s.length < 9 ? 1 : s.length < 20 ? 2 : 3;

  function compare(a, e, lang) {
    if (a === e) return [5, "correct"];
    const fa = fold(a), fe = fold(e);
    if (lang === "de") {
      if (fa === fe) return [4, "umlaut"];
      const m = e.match(ARTICLE_RE);
      if (m) {
        const [, article, noun] = m;
        if (a === noun || fa === fold(noun)) return [2, "article_missing"];
        const ma = a.match(ARTICLE_RE);
        if (ma && ma[1] !== article && lev(fold(ma[2]), fold(noun)) <= allowedTypos(noun)) return [1, "wrong_article"];
      }
      if (e.startsWith("sich ") && a === e.slice(5)) return [3, "sich_missing"];
    }
    if (lev(fa, fe) <= allowedTypos(fe)) return [3, "typo"];
    return [0, "wrong"];
  }

  function bestMatch(answer, expected, lang) {
    const a = norm(answer, lang);
    if (!a) return [0, "unknown", null];
    let best = [-1, "wrong", null];
    for (const exp of expected) {
      const [q, v] = compare(a, norm(exp, lang), lang);
      if (q > best[0]) best = [q, v, exp];
    }
    return best;
  }

  function wordType(w) {
    const de0 = alts(w.de)[0], en0 = alts(w.en)[0];
    if (ARTICLE_RE.test(de0.toLowerCase())) return "noun";
    const n = de0.split(/\s+/).length;
    if (n >= 3) return "idiom";
    if (en0.toLowerCase().startsWith("to ")) return n <= 2 ? "verb" : "idiom";
    return "adj / adv";
  }
  const mask = s => s.split(/\s+/).map(w => w[0] + "_".repeat(w.length - 1)).join(" ");

  const VERDICT_TEXT = {
    correct: "Richtig!",
    umlaut: "Correct – but write the umlauts (ä ö ü ß).",
    typo: "Almost – small spelling mistake.",
    sich_missing: "Almost – it's reflexive, don't forget 'sich'.",
    article_missing: "Include the article (der / die / das) – gender matters at C2!",
    wrong_article: "Wrong article.",
    wrong: "Not quite.",
    unknown: "Here's the answer.",
    synonym: "That's a valid synonym – but a different word is wanted. Try again!",
  };

  // ---------------------------------------------------------------- scheduling (SM-2 variant)
  function schedule(p, q, t) {
    let {reps, ease, interval, lapses} = p;
    if (q < 3) {
      if (reps > 0) lapses += 1;
      reps = 0;
      ease = Math.max(1.3, ease - 0.2);
      interval = q <= 1 ? 2 * MIN : 6 * MIN;
    } else {
      reps += 1;
      if (reps === 1) interval = q >= 4 ? DAY : 10 * MIN;
      else if (reps === 2) interval = q >= 4 ? 3 * DAY : DAY;
      else interval = Math.max(interval, DAY) * ease * {3: 0.8, 4: 1.0, 5: 1.15}[q];
      ease = Math.max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02));
      if (interval >= DAY) interval *= 0.93 + Math.random() * 0.14;
    }
    return {reps, ease, interval, lapses, due: t + interval};
  }

  function human(sec) {
    if (sec < 90 * MIN) return `${Math.max(1, Math.round(sec / MIN))} min`;
    if (sec < 1.5 * DAY) return `${Math.round(sec / 3600)} h`;
    if (sec < 60 * DAY) return `${Math.round(sec / DAY)} days`;
    return `${Math.round(sec / (30 * DAY))} months`;
  }

  function dayStart(daysAgo = 0) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - daysAgo);
    return d.getTime() / 1000;
  }

  // ---------------------------------------------------------------- core actions
  function filters() {
    const s = state.settings;
    return {levels: new Set(s.levels.length ? s.levels : LEVELS), dirs: s.mode === "mixed" ? DIRECTIONS : [s.mode]};
  }

  function cards() {
    const {levels, dirs} = filters(), out = [];
    for (const [k, p] of Object.entries(state.progress)) {
      const [de, dir] = k.split("\t"), w = byDe.get(de);
      if (w && levels.has(w.level) && dirs.includes(dir)) out.push({w, dir, p});
    }
    return out;
  }

  function summary() {
    const t = now(), today = dayStart();
    let reviews = 0, correct = 0;
    for (const l of state.log) if (l[0] >= today) { reviews++; if (l[3] >= 3) correct++; }
    return {
      due: cards().filter(c => c.p.due <= t).length,
      new_today: Object.values(state.progress).filter(p => p.first_seen >= today).length,
      new_limit: state.settings.new_per_day, today_reviews: reviews, today_correct: correct,
    };
  }

  function makeCard(w, direction, kind) {
    return {word_id: w.id, direction, prompt: alts(direction === "de2en" ? w.de : w.en).join(" / "),
            level: w.level, kind, type: wordType(w)};
  }

  function nextCard(exclude) {
    const t = now(), {levels, dirs} = filters();
    const all = cards().filter(c => c.w.id !== exclude);
    const newOne = () => {
      if (summary().new_today >= state.settings.new_per_day) return null;
      for (const d of shuffle([...dirs])) {
        const cand = [];
        for (const w of byRank) {
          if (!levels.has(w.level) || w.id === exclude || state.progress[key(w.de, d)]) continue;
          cand.push(w);
          if (cand.length >= 40) break;
        }
        if (cand.length) return makeCard(pick(cand), d, "new");
      }
      return null;
    };
    const due = all.filter(c => c.p.due <= t).sort((a, b) => a.p.due - b.p.due).slice(0, 5);
    if (due.length) {
      if (Math.random() < 0.2) { const c = newOne(); if (c) return c; }
      const r = pick(due);
      return makeCard(r.w, r.dir, "review");
    }
    const c = newOne();
    if (c) return c;
    const weak = all.sort((a, b) => (b.p.wrong - 0.5 * b.p.correct) - (a.p.wrong - 0.5 * a.p.correct)
                                    || a.p.ease - b.p.ease || a.p.due - b.p.due).slice(0, 12);
    if (weak.length) { const r = pick(weak); return makeCard(r.w, r.dir, "practice"); }
    return null;
  }

  function checkAnswer(w, direction, answer, hinted) {
    const [target, lang] = direction === "de2en" ? [w.en, "en"] : [w.de, "de"];
    let [q, verdict, matched] = bestMatch(answer, alts(target), lang);
    if (q < 3 && direction === "en2de" && answer.trim()) {
      // Did they give a different German word that means the same thing?
      if (!enCache) enCache = words.map(o => [o, new Set(alts(o.en).map(e => norm(e, "en")))]);
      const wanted = new Set(alts(w.en).map(e => norm(e, "en")));
      for (const [o, ens] of enCache) {
        if (o.id === w.id || ![...wanted].some(e => ens.has(e))) continue;
        const [oq, , om] = bestMatch(answer, alts(o.de), "de");
        if (oq >= 3) return {verdict: "synonym", message: VERDICT_TEXT.synonym, synonym_of: om};
      }
    }
    if (hinted && q > 3) q = 3;
    return {quality: q, verdict, message: VERDICT_TEXT[verdict], matched,
            de: alts(w.de).join(" / "), en: alts(w.en).join(" / "),
            example: w.example, example_en: w.example_en, level: w.level};
  }

  function grade(w, direction, q, answer) {
    const t = now();
    q = Math.max(0, Math.min(5, parseInt(q)));
    const k = key(w.de, direction);
    let p = state.progress[k];
    if (!p) p = state.progress[k] = {reps: 0, ease: 2.5, interval: 0, due: t, correct: 0, wrong: 0, lapses: 0,
                                     first_seen: t, last_seen: t};
    const practice = p.due > t && p.reps > 0;
    let nextIn;
    if (practice && q >= 3) nextIn = p.due - t;   // extra practice: don't inflate the interval
    else { Object.assign(p, schedule(p, q, t)); nextIn = p.interval; }
    if (q >= 3) p.correct++; else p.wrong++;
    p.last_seen = t;
    state.log.push([t, w.de, direction, q]);
    return {next_in: human(nextIn)};
  }

  function stats() {
    const t = now();
    const perLevel = LEVELS.map(level => {
      const ws = words.filter(w => w.level === level).length;
      let seen = 0, mastered = 0, learning = 0;
      for (const [k, p] of Object.entries(state.progress)) {
        const w = byDe.get(k.split("\t")[0]);
        if (!w || w.level !== level) continue;
        seen++; if (p.interval >= MASTERED) mastered++; if (p.interval < DAY) learning++;
      }
      return {level, cards: ws * 2, seen, mastered, learning};
    });
    const inDay = (ts, i) => ts >= dayStart(i) && ts < dayStart(i - 1);
    const days = [];
    for (let i = 13; i >= 0; i--) {
      const ls = state.log.filter(l => inDay(l[0], i));
      const d = new Date(dayStart(i) * 1000);
      const date = d.toLocaleDateString("en-GB", {weekday: "short"}) + " " +
                   String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + ".";
      days.push({date, reviews: ls.length, correct: ls.filter(l => l[3] >= 3).length});
    }
    let streak = 0, i = days[13].reviews ? 0 : 1;
    while (state.log.some(l => inDay(l[0], i))) { streak++; i++; }
    const hardest = Object.entries(state.progress)
      .map(([k, p]) => ({k, p, w: byDe.get(k.split("\t")[0])}))
      .filter(x => x.w && x.p.wrong > 0)
      .sort((a, b) => b.p.wrong / (b.p.correct + b.p.wrong) - a.p.wrong / (a.p.correct + a.p.wrong) || b.p.wrong - a.p.wrong)
      .slice(0, 20)
      .map(({k, p, w}) => ({id: w.id, de: w.de, en: w.en, level: w.level, direction: k.split("\t")[1],
                            correct: p.correct, wrong: p.wrong, lapses: p.lapses, ease: p.ease}));
    return {summary: summary(), per_level: perLevel, days, streak, hardest,
            all_reviews: state.log.length, all_correct: state.log.filter(l => l[3] >= 3).length,
            due_next_24h: Object.values(state.progress).filter(p => p.due > t && p.due <= t + DAY).length,
            settings: state.settings};
  }

  function listWords(q) {
    q = q.toLowerCase();
    const found = words.filter(w => w.de.toLowerCase().includes(q) || w.en.toLowerCase().includes(q))
      .sort((a, b) => a.de.toLowerCase() < b.de.toLowerCase() ? -1 : a.de.toLowerCase() > b.de.toLowerCase() ? 1 : 0)
      .slice(0, 500);
    return {total: words.length, words: found.map(w => {
      const d = {id: w.id, de: w.de, en: w.en, level: w.level, example: w.example, example_en: w.example_en};
      for (const dir of DIRECTIONS) {
        const p = state.progress[key(w.de, dir)];
        if (p) d[dir] = {correct: p.correct, wrong: p.wrong, interval: p.interval ? human(p.interval) : "-",
                         mastered: p.interval >= MASTERED};
      }
      return d;
    })};
  }

  function forget(de) {
    for (const dir of DIRECTIONS) delete state.progress[key(de, dir)];
  }

  function saveWord(data) {
    const de = (data.de || "").trim(), en = (data.en || "").trim();
    if (!de || !en) fail("German and English are both required.");
    const w = {de, en, level: LEVELS.includes(data.level) ? data.level : "C1",
               example: (data.example || "").trim(), example_en: (data.example_en || "").trim()};
    if (data.id !== null && data.id !== undefined && data.id !== "") {
      const old = words[+data.id];
      if (old && old.de !== de) {   // renamed: keep its progress under the new name
        for (const dir of DIRECTIONS) {
          const p = state.progress[key(old.de, dir)];
          if (p) state.progress[key(de, dir)] = p;
        }
        forget(old.de);
        delete state.custom[old.de];
        if (!state.deleted.includes(old.de)) state.deleted.push(old.de);
      }
    } else if (byDe.has(de)) {
      fail(`'${de}' is already in the list.`);
    }
    state.deleted = state.deleted.filter(d => d !== de);
    state.custom[de] = w;
    rebuild();
    return {id: byDe.get(de).id};
  }

  function deleteWord(id) {
    const w = words[id];
    if (w) {
      forget(w.de);
      delete state.custom[w.de];
      if (!state.deleted.includes(w.de)) state.deleted.push(w.de);
      rebuild();
    }
    return {ok: true};
  }

  function saveSettings(d) {
    const s = state.settings;
    if (d.levels) s.levels = d.levels.filter(l => LEVELS.includes(l)).length ? d.levels.filter(l => LEVELS.includes(l)) : LEVELS;
    if (["mixed", ...DIRECTIONS].includes(d.mode)) s.mode = d.mode;
    if (d.new_per_day !== undefined) s.new_per_day = Math.max(0, Math.min(500, parseInt(d.new_per_day) || 0));
    return s;
  }

  function importRows(text) {
    const delim = text.includes("\t") && !text.includes(";") ? "\t" : ";";
    let parsed = 0, added = 0;
    for (const line of text.split(/\r?\n/)) {
      const f = line.split(delim).map(x => x.trim().replace(/^"(.*)"$/, "$1"));
      if (f.length < 2 || !f[0] || f[0].startsWith("#") || ["german", "de", "deutsch"].includes(f[0].toLowerCase())) continue;
      parsed++;
      if (byDe.has(f[0])) continue;
      const level = LEVELS.includes((f[2] || "").toUpperCase()) ? f[2].toUpperCase() : "C1";
      state.deleted = state.deleted.filter(d => d !== f[0]);
      state.custom[f[0]] = {de: f[0], en: f[1], level, example: f[3] || "", example_en: f[4] || ""};
      byDe.set(f[0], true);   // so duplicates inside the pasted text are skipped
      added++;
    }
    rebuild();
    return {parsed, added};
  }

  // ---------------------------------------------------------------- progress.json (same format as app.py)
  function pyJson(x) {   // Python-style json.dumps spacing, so the file diffs cleanly across devices
    if (Array.isArray(x)) return "[" + x.map(pyJson).join(", ") + "]";
    if (x && typeof x === "object") return "{" + Object.entries(x).map(([k, v]) => JSON.stringify(k) + ": " + pyJson(v)).join(", ") + "}";
    return JSON.stringify(x);
  }
  const r3 = v => Number.isInteger(v) ? v : Math.round(v * 1000) / 1000;

  function exportText() {
    const progress = Object.entries(state.progress)
      .filter(([k]) => byDe.has(k.split("\t")[0]))
      .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
      .map(([k, p]) => {
        const [de, dir] = k.split("\t");
        return {de, dir, ...Object.fromEntries(PROGRESS_FIELDS.map(f => [f, r3(p[f])]))};
      });
    const log = state.log.filter(l => byDe.has(l[1])).sort((a, b) => a[0] - b[0]);
    const custom = Object.values(state.custom).filter(w => byDe.has(w.de)).sort((a, b) => a.de < b.de ? -1 : 1)
      .map(w => Object.fromEntries(WORD_FIELDS.map(f => [f, w[f] || ""])));
    const deleted = [...state.deleted].sort();
    const block = (name, items) => items.length
      ? `  "${name}": [\n${items.map(x => "    " + pyJson(x)).join(",\n")}\n  ]` : `  "${name}": []`;
    return "{\n" + ['  "version": 1', `  "settings": ${pyJson(state.settings)}`, block("words", custom),
                    block("deleted", deleted), block("progress", progress), block("log", log)].join(",\n") + "\n}\n";
  }

  function mergeJson(data) {
    for (const de of data.deleted || []) {
      if (!state.deleted.includes(de)) state.deleted.push(de);
      delete state.custom[de];
      forget(de);
    }
    for (const w of data.words || []) {
      state.custom[w.de] = Object.fromEntries(WORD_FIELDS.map(f => [f, w[f] || ""]));
      state.deleted = state.deleted.filter(d => d !== w.de);
    }
    rebuild();
    let updated = 0, entries = 0;
    for (const p of data.progress || []) {
      if (!byDe.has(p.de)) continue;
      const k = key(p.de, p.dir), cur = state.progress[k];
      if (cur && cur.last_seen >= p.last_seen) continue;
      state.progress[k] = Object.fromEntries(PROGRESS_FIELDS.map(f => [f, p[f]]));
      updated++;
    }
    const have = new Set(state.log.map(l => `${l[0]}\t${l[1]}\t${l[2]}`));
    for (const [ts, de, dir, q] of data.log || []) {
      const k = `${ts}\t${de}\t${dir}`;
      if (!byDe.has(de) || have.has(k)) continue;
      have.add(k);
      state.log.push([ts, de, dir, q]);
      entries++;
    }
    state.log.sort((a, b) => a[0] - b[0]);
    if (data.settings) saveSettings(data.settings);
    return `Imported: ${updated} words updated, ${entries} new history entries.`;
  }

  // ---------------------------------------------------------------- request router (mirrors app.py)
  async function request(path, method = "GET", body = {}) {
    await ready;
    const url = new URL(path, location.href), p = url.pathname.replace(/^.*\/api\//, "/api/");
    const qs = Object.fromEntries(url.searchParams);
    const word = id => words[+id] || fail("Word not found.");
    let out, changed = method !== "GET";
    if (method === "GET" && p === "/api/next") {
      const ex = /^\d+$/.test(qs.exclude || "") ? +qs.exclude : null;
      out = {card: nextCard(ex), summary: summary()};
    } else if (method === "GET" && p === "/api/stats") out = stats();
    else if (method === "GET" && p === "/api/words") out = listWords(qs.q || "");
    else if (method === "GET" && p === "/api/hint") {
      const w = word(qs.word_id);
      out = {hint: mask(alts(qs.direction === "de2en" ? w.en : w.de)[0])};
    } else if (method === "POST" && p === "/api/check") {
      changed = false;
      out = checkAnswer(word(body.word_id), body.direction, body.answer || "", body.hinted || false);
    } else if (method === "POST" && p === "/api/grade") out = grade(word(body.word_id), body.direction, body.quality, body.answer || "");
    else if (method === "POST" && p === "/api/settings") out = saveSettings(body);
    else if (method === "POST" && p === "/api/words") out = saveWord(body);
    else if (method === "POST" && p === "/api/import") out = importRows(body.text || "");
    else if (method === "DELETE" && p.startsWith("/api/words/")) out = deleteWord(+p.split("/").pop());
    else fail("not found");
    if (changed) await persist();
    return JSON.parse(JSON.stringify(out));
  }

  async function exportFile() {
    await ready;
    const file = new File([exportText()], "progress.json", {type: "application/json"});
    if (navigator.canShare && navigator.canShare({files: [file]})) {
      try { await navigator.share({files: [file], title: "progress.json"}); return; }
      catch (e) { if (e.name === "AbortError") return; }
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(file);
    a.download = "progress.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  async function importFile(file) {
    await ready;
    let data;
    try { data = JSON.parse(await file.text()); } catch { fail("That file is not valid progress.json."); }
    if (!data || typeof data !== "object" || !("progress" in data)) fail("That file is not valid progress.json.");
    const msg = mergeJson(data);
    await persist();
    return msg;
  }

  window.LocalEngine = {request, exportFile, importFile, _test: {norm, compare, bestMatch, schedule, exportText, mergeJson}};
})();
