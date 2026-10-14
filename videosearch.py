"""Поиск по словам в видеоархиве.

Команды:
  index   папка_с_видео          распознать речь во всех видео (можно прерывать и запускать снова)
  search  "слово или фраза"      найти видео и скопировать в папку результатов
  report                         показать дубли, ролики без речи и иностранные ролики

Примеры:
  python videosearch.py index D:\\video
  python videosearch.py search "кошк*" --out D:\\found
  python videosearch.py search "тёмная сторона силы"
"""
import argparse
import csv
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts"}
DB_NAME = "video_index.db"
MIN_SPEECH_SEC = 3.0      # меньше речи, чем это, считаем роликом без слов (музыка, природа)
FOREIGN_PROB = 0.6        # насколько уверенно модель должна определить не русский язык

_morph = None


def morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph


_lemma_cache = {}


def lemma(word):
    word = word.lower().replace("ё", "е")
    r = _lemma_cache.get(word)
    if r is None:
        r = morph().parse(word)[0].normal_form.replace("ё", "е")
        _lemma_cache[word] = r
    return r


def tokens(text):
    return re.findall(r"[а-яёa-z0-9]+", text.lower())


def lemmas(text):
    return [lemma(t) for t in tokens(text)]


def connect(db_dir):
    db = sqlite3.connect(os.path.join(db_dir, DB_NAME))
    db.executescript("""
    CREATE TABLE IF NOT EXISTS videos(
        path TEXT PRIMARY KEY, size INTEGER, mtime REAL, fhash TEXT,
        status TEXT, lang TEXT, duration REAL, text TEXT);
    CREATE TABLE IF NOT EXISTS words(
        path TEXT, idx INTEGER, start REAL, lemma TEXT);
    CREATE INDEX IF NOT EXISTS words_lemma ON words(lemma);
    """)
    return db


def file_hash(path):
    """Быстрый отпечаток: размер + начало + середина + конец файла. Совпал отпечаток, значит дубль."""
    size = os.path.getsize(path)
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        for pos in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
            f.seek(pos)
            h.update(f.read(1048576))
    return h.hexdigest()


def find_videos(root):
    for dp, _, files in os.walk(root):
        for n in files:
            if os.path.splitext(n)[1].lower() in VIDEO_EXT:
                yield os.path.join(dp, n)


def load_model(name, device):
    from faster_whisper import WhisperModel
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    print(f"Модель {name}, устройство {device}")
    return WhisperModel(name, device=device, compute_type=compute)


def process_one(model, path):
    """Возвращает (status, lang, duration, text, [(start, word), ...])."""
    from faster_whisper.audio import decode_audio
    audio = decode_audio(path, sampling_rate=16000)
    duration = len(audio) / 16000
    if duration < 0.5:
        return "no_speech", "", duration, "", []
    lang, prob, _ = model.detect_language(audio[:16000 * 30])
    if lang != "ru" and prob >= FOREIGN_PROB:
        return "foreign", lang, duration, "", []
    segments, info = model.transcribe(
        audio, language="ru", vad_filter=True, beam_size=1,
        condition_on_previous_text=False, word_timestamps=True)
    words, parts = [], []
    for seg in segments:
        parts.append(seg.text.strip())
        for w in seg.words or []:
            for t in tokens(w.word):
                words.append((w.start, t))
    speech = getattr(info, "duration_after_vad", duration)
    if speech < MIN_SPEECH_SEC or len(words) < 3:
        return "no_speech", "ru", duration, " ".join(parts), []
    return "ok", "ru", duration, " ".join(parts), words


def cmd_index(a):
    root = os.path.abspath(a.folder)
    db_dir = os.path.abspath(a.db) if a.db else root
    db = connect(db_dir)
    files = sorted(find_videos(root))
    print(f"Найдено видео: {len(files)}")
    model = None
    done = skipped = 0
    t0 = time.time()
    for i, path in enumerate(files, 1):
        st = os.stat(path)
        row = db.execute("SELECT size, mtime, status FROM videos WHERE path=?", (path,)).fetchone()
        if row and row[2] != "error" and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1:
            skipped += 1
            continue
        if model is None:
            model = load_model(a.model, a.device)
        try:
            fh = file_hash(path)
            status, lang, dur, text, words = process_one(model, path)
        except Exception as e:
            print(f"[{i}/{len(files)}] ОШИБКА {path}: {e}")
            status, lang, dur, text, words, fh = "error", "", 0, str(e), [], ""
        db.execute("DELETE FROM words WHERE path=?", (path,))
        db.execute("INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?,?,?,?)",
                   (path, st.st_size, st.st_mtime, fh, status, lang, dur, text))
        db.executemany("INSERT INTO words VALUES(?,?,?,?)",
                       [(path, k, s, lemma(w)) for k, (s, w) in enumerate(words)])
        db.commit()
        done += 1
        print(f"[{i}/{len(files)}] {status:9s} {dur:6.1f} c  {os.path.basename(path)}")
    print(f"Готово. Обработано: {done}, пропущено (уже в индексе): {skipped}, "
          f"время {(time.time() - t0) / 60:.1f} мин.")


def build_matcher(query):
    """Фраза -> список шаблонов по словам. 'кошк*' ищет по началу слова, остальные слова по лемме."""
    pats = []
    for raw in query.split():
        if "*" in raw:
            rx = re.compile("^" + ".*".join(re.escape(p) for p in raw.lower().replace("ё", "е").split("*")) + "$")
            pats.append(("rx", rx))
        else:
            for t in tokens(raw):
                pats.append(("lemma", lemma(t)))
    return pats


def word_ok(pat, token_text, token_lemma):
    kind, val = pat
    if kind == "lemma":
        return token_lemma == val
    return bool(val.match(token_text.replace("ё", "е"))) or bool(val.match(token_lemma))


def cmd_search(a):
    db = connect(os.path.abspath(a.db))
    pats = build_matcher(a.query)
    if not pats:
        sys.exit("Пустой запрос")
    hits = []
    for path, text in db.execute("SELECT path, text FROM videos WHERE status='ok'"):
        toks = tokens(text)
        lems = [lemma(t) for t in toks]
        # позиции слов с таймкодами берём из таблицы words
        starts = [r[0] for r in db.execute("SELECT start FROM words WHERE path=? ORDER BY idx", (path,))]
        n = len(pats)
        for i in range(len(toks) - n + 1):
            if all(word_ok(pats[k], toks[i + k], lems[i + k]) for k in range(n)):
                t = starts[i] if i < len(starts) else 0
                ctx = " ".join(toks[max(0, i - 4): i + n + 4])
                hits.append((path, t, ctx))
                break
    print(f"Найдено видео: {len(hits)}")
    out = os.path.abspath(a.out) if a.out else None
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "результат.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["файл", "время, с", "контекст"])
            for path, t, ctx in hits:
                w.writerow([path, f"{t:.1f}", ctx])
    for path, t, ctx in hits:
        mm, ss = divmod(int(t), 60)
        print(f"  {os.path.basename(path)}  [{mm}:{ss:02d}]  ...{ctx}...")
        if out:
            dest = os.path.join(out, os.path.basename(path))
            k = 1
            while os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(path))
                dest = os.path.join(out, f"{base}_{k}{ext}")
                k += 1
            shutil.copy2(path, dest)
    if out:
        print(f"Скопировано в {out}")


def cmd_report(a):
    db = connect(os.path.abspath(a.db))
    out = os.path.abspath(a.out) if a.out else None
    dups = db.execute("""SELECT fhash, GROUP_CONCAT(path, ' | ') FROM videos
                         WHERE fhash<>'' GROUP BY fhash HAVING COUNT(*)>1""").fetchall()
    print(f"Группы дублей: {len(dups)} (ничего не удаляю, только показываю)")
    for _, ps in dups:
        print("  ", ps)
    for status, title in (("no_speech", "Без речи (музыка, природа)"), ("foreign", "Иностранные"), ("error", "Ошибки")):
        rows = db.execute("SELECT path, lang FROM videos WHERE status=?", (status,)).fetchall()
        print(f"{title}: {len(rows)}")
        if out and rows:
            d = os.path.join(os.path.abspath(out), status)
            os.makedirs(d, exist_ok=True)
            for p, _ in rows:
                shutil.copy2(p, os.path.join(d, os.path.basename(p)))
    if out:
        print(f"Копии без речи/иностранных/ошибок сложены в {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index")
    p.add_argument("folder")
    p.add_argument("--db", help="куда положить индекс (по умолчанию в папку с видео)")
    p.add_argument("--model", default="small", help="tiny, base, small, medium, large-v3")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--db", default=".", help="папка, где лежит индекс")
    p.add_argument("--out", help="папка, куда копировать найденные видео")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("report")
    p.add_argument("--db", default=".")
    p.add_argument("--out", help="сложить копии роликов без речи и иностранных сюда")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
