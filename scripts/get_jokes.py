#!/usr/bin/env python3
"""Скачать анекдоты для plugin_jokes в data/jokes.json.

Источники (свободные):
  hf          — датасет igorktech/anekdots на Hugging Face (лицензия ODC-BY; анекдоты с anekdot.ru 1996–2023,
                ~500 тыс. штук с оценками читателей). Берём самые высоко оценённые — через открытый API
                datasets-server.huggingface.co, без скачивания всего датасета (~120 МБ).
  rzhunemogu  — rzhunemogu.ru: случайные анекдоты, отдельная категория «для взрослых».

Анекдоты делятся на обычные и пошлые (по словам-признакам; мат — тоже в «пошлые»). Выбрасываются:
  * пошлые анекдоты, где фигурируют дети (Вовочка, школа, сын, дочка...);
  * анекдоты на национальные и прочие оскорбительные стереотипы;
  * слишком длинные для озвучки и с латиницей (Silero её не читает).

Запуск (на приставке, из корня репозитория):
    .venv/bin/python scripts/get_jokes.py                    # hf, 3000 обычных и 1500 пошлых
    .venv/bin/python scripts/get_jokes.py --normal 5000 --adult 2000
    .venv/bin/python scripts/get_jokes.py --source rzhunemogu --normal 500 --adult 300
    .venv/bin/python scripts/get_jokes.py --source all        # оба источника вместе
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "jokes.json")

HF_API = "https://datasets-server.huggingface.co"
HF_DATASET = "igorktech/anekdots"
RZH_URL = "http://rzhunemogu.ru/RandJSON.aspx"
UA = {"User-Agent": "IreneVoiceAssistant/1.0 (jokes downloader)"}


def _rx(*stems):
    return re.compile("|".join(stems))


# ---------------------------------------------------------------- признаки (текст в нижнем регистре, «ё» -> «е»)
ADULT = _rx(
    r"секс", r"постел", r"любовни", r"изменя(ет|ла|л\b|ешь|ю\b|ть)", r"изменил(а\b|\b)", r"\bизмен[ау]\b", r"\bгол(ая|ый|ой|ую|ые|ым|ыми|ышом)\b",
    r"\bтрус(ы|ах|ики|иках|иков|ишки)\b", r"лифчик", r"бюстгальтер", r"сиськ", r"\bжоп", r"задниц",
    r"минет", r"оргазм", r"презерватив", r"эрекц", r"потенц", r"импотен", r"виагр", r"проститут", r"путан",
    r"бордел", r"публичн\w* дом", r"стриптиз", r"порн", r"эроти", r"кама.?сутр", r"девственн", r"переспа",
    r"трахн", r"трахал", r"трахат", r"интим", r"возбужд", r"раздева", r"раздел(ась|ся|ись)", r"брачн\w* ноч",
    r"гинеколог", r"уролог", r"венеролог", r"сифилис", r"триппер", r"соблазн", r"страстн", r"заня\w* любовью",
    r"занима\w* любовью", r"ширинк", r"\bписьк", r"пипис", r"сперм", r"дроч", r"любовн\w* утех",
    r"ночь любви", r"на любовника", r"жена.{0,40}любовник", r"муж.{0,40}любовниц",
)
MAT = _rx(
    r"\bху[йяеи]", r"пизд", r"(^|[^а-я])(за|вы|у|на|от|по|при|раз|до|про|съ|въ|подъ|объ|отъ|разъ)?еб(ат|ал|ан|ут|у\b|ыр|ли|л\b|нул|нут|ись)",
    r"\bбля(дь|ть|д|\b)", r"\bмуда", r"мудак", r"залуп", r"\bманд[аеуы]\b", r"\bхер\b", r"\bсук[аи]\b",
)
CHILDREN = _rx(
    r"вовочк", r"школ", r"ребен", r"\bдет(и|ей|ям|ьми|ях|ки|ок|ишк|ск)", r"\bсын", r"\bдоч", r"\bвнук", r"\bвнучк",
    r"малыш", r"мальчик", r"мальчишк", r"девочк", r"девчонк", r"девчушк", r"подрост", r"несовершеннолет",
    r"малолет", r"учительниц", r"первоклас", r"детсад", r"детск\w* сад", r"младен", r"\bпионер", r"одноклассн",
)
OFFENSIVE = _rx(
    r"\bжид", r"\bхач", r"\bчурк", r"чучмек", r"черномаз", r"\bниг+ер", r"\bнегр", r"\bхохл", r"\bхохол",
    r"\bкацап", r"\bмоскал", r"узкоглаз", r"\bпид[оа]?р", r"\bпедик", r"педераст", r"\bгомик", r"\bге(й|и|ев|ям)\b", r"\bгейск", r"\bлесби",
    r"\bдаун", r"изнасил", r"насилу", r"насилов", r"педофил",
    r"\bчукч", r"\bевре", r"рабинович", r"\bабрам", r"\bизя\b", r"\bмоня\b", r"\bхаим", r"\bсар[аеуы]\b",
    r"\bармян", r"\bгрузин", r"\bкавказ", r"\bцыган", r"\bтаджик", r"\bузбек", r"гастарбайтер", r"\bазиат",
    r"\bнацист", r"гитлер", r"холокост", r"освенцим", r"теракт", r"террорист",
)


def norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = text.replace("\r", "")
    lines = [ln.strip(" \t-—–") for ln in text.split("\n")]
    text = " ".join(ln for ln in lines if ln)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def classify(text: str, adult_hint: bool = False):
    """-> "normal" | "adult" | None (не подходит)."""
    if not 40 <= len(text) <= 600:
        return None
    if len(re.findall(r"[A-Za-z]", text)) > 3:          # Silero латиницу не читает — пропадёт половина шутки
        return None
    if len(re.findall(r"[А-Яа-яЁё]", text)) < len(text) * 0.6:
        return None
    t = " " + norm(text) + " "
    if OFFENSIVE.search(t):
        return None
    adult = adult_hint or bool(ADULT.search(t)) or bool(MAT.search(t))
    if adult and CHILDREN.search(t):
        return None
    return "adult" if adult else "normal"


def key_of(text: str) -> str:
    return hashlib.md5(re.sub(r"[^а-я]", "", norm(text)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- Hugging Face
def _get_json(url, params=None, tries=5):
    delay = 2.0
    for n in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if n == tries - 1:
                raise
            print(f"   повтор через {delay:.0f} с ({e})")
            time.sleep(delay)
            delay *= 2


def from_hf(pools, want, max_rows):
    print(f"==> Hugging Face: {HF_DATASET} (ODC-BY), лучшие по оценке читателей")
    offset, page, seen_rows = 0, 100, 0
    use_filter = True
    while seen_rows < max_rows and (len(pools["normal"]) < want["normal"] or len(pools["adult"]) < want["adult"]):
        params = {"dataset": HF_DATASET, "config": "default", "split": "train", "offset": offset, "length": page}
        try:
            if use_filter:
                params.update({"where": '"total_mark">0', "orderby": '"total_mark" DESC'})
                data = _get_json(HF_API + "/filter", params)
            else:
                data = _get_json(HF_API + "/rows", params)
        except Exception as e:
            if use_filter:
                print(f"   сортировка по оценке недоступна ({e}) — беру подряд, отбирая хорошие оценки")
                use_filter, offset = False, 0
                continue
            print(f"!! Hugging Face недоступен: {e}")
            return
        rows = data.get("rows") or []
        if not rows:
            break
        for item in rows:
            row = item.get("row", item)
            mark = row.get("total_mark") or 0
            if not use_filter and mark < 50:
                continue
            add(pools, want, clean(row.get("text", "")), mark=mark)
        seen_rows += len(rows)
        offset += page
        print(f"\r   просмотрено {seen_rows}: обычных {len(pools['normal'])}, пошлых {len(pools['adult'])}",
              end="", flush=True)
        time.sleep(0.2)
    print()


# ---------------------------------------------------------------- rzhunemogu.ru
def from_rzhunemogu(pools, want):
    print("==> rzhunemogu.ru (случайные анекдоты)")
    for kind, ctype in (("normal", 1), ("adult", 11)):
        misses, got0 = 0, len(pools[kind])
        while len(pools[kind]) < want[kind] and misses < 200:
            try:
                r = requests.get(RZH_URL, params={"CType": ctype}, headers=UA, timeout=15)
                raw = r.content.decode("cp1251", errors="replace")
                # JSON у них бывает с неэкранированными переводами строк — разбираем сами
                m = re.search(r'"content"\s*:\s*"(.*)"\s*}', raw, re.S)
                text = clean(m.group(1).replace('\\"', '"')) if m else ""
            except Exception as e:
                print(f"\n   ошибка: {e}")
                misses += 5
                time.sleep(2)
                continue
            before = len(pools["normal"]) + len(pools["adult"])
            add(pools, want, text, adult_hint=(ctype == 11))
            misses = 0 if len(pools["normal"]) + len(pools["adult"]) > before else misses + 1
            print(f"\r   {'обычных' if kind == 'normal' else 'пошлых'}: {len(pools[kind]) - got0}", end="", flush=True)
            time.sleep(0.3)
        print()


_seen = set()


def add(pools, want, text, adult_hint=False, mark=None):
    kind = classify(text, adult_hint)
    if kind is None or len(pools[kind]) >= want[kind]:
        return
    k = key_of(text)
    if k in _seen:
        return
    _seen.add(k)
    pools[kind].append(text)


def main():
    ap = argparse.ArgumentParser(description="Скачать анекдоты для Ирины")
    ap.add_argument("--source", choices=["hf", "rzhunemogu", "all"], default="hf")
    ap.add_argument("--normal", type=int, default=3000, help="сколько обычных анекдотов")
    ap.add_argument("--adult", type=int, default=1500, help="сколько пошлых анекдотов")
    ap.add_argument("--max-rows", type=int, default=40000, help="сколько строк датасета просмотреть, не больше")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    pools = {"normal": [], "adult": []}
    want = {"normal": a.normal, "adult": a.adult}
    sources = []
    if a.source in ("hf", "all"):
        from_hf(pools, want, a.max_rows)
        sources.append("Hugging Face igorktech/anekdots (ODC-BY, тексты с anekdot.ru)")
    if a.source in ("rzhunemogu", "all") or (a.source == "hf" and not pools["normal"]):
        if a.source == "hf":
            print("   с Hugging Face ничего не пришло — пробую rzhunemogu.ru")
            want = {"normal": min(a.normal, 500), "adult": min(a.adult, 300)}
        from_rzhunemogu(pools, want)
        sources.append("rzhunemogu.ru")
    if not pools["normal"] and not pools["adult"]:
        print("!! Ничего не скачалось: проверьте интернет на приставке")
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    data = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "sources": sources,
            "normal": pools["normal"], "adult": pools["adult"]}
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    os.replace(tmp, a.out)
    print(f"==> Готово: {a.out} — обычных {len(pools['normal'])}, пошлых {len(pools['adult'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
