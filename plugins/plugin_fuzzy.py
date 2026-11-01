# Нечёткое сравнение команд: «вкличи свет», «играй больше меньше», «поставь тайгер» — находит ближайшую команду,
# если распознавание чуть ошиблось или команда сказана другой формой слова.
# Срабатывает только когда точного совпадения нет (точное и «начало фразы» ищет ядро до нечётких сравнителей).
# Без внешних библиотек: difflib из стандартной библиотеки Python.

import difflib
import logging
import os

from vacore import VACore

modname = os.path.basename(__file__)[:-3]
logger = logging.getLogger(__name__)
_options = {}


def start(core: VACore):
    return {
        "name": "Нечёткое сравнение команд",
        "version": "1.0",
        "require_online": False,
        "description": "Находит ближайшую команду при ошибках распознавания (difflib, без внешних библиотек).",
        "default_options": {
            "threshold": 0.78,     # насколько похоже (0..1): ниже — «не поняла»
            "ambiguity": 0.06,     # две разные команды почти одинаково похожи — не угадываем, переспросим
            "min_key_len": 4,      # команды короче (например «да») — только точное совпадение
            "sure": 0.93,          # почти точное совпадение: из таких берём команду, покрывающую больше слов
                                   # («включи мою вону» -> «включи мою волну», а не «включи» + «мою вону»)
        },
        "fuzzy_processor": {
            "difflib": (init, process),
        },
    }


def start_with_options(core: VACore, manifest: dict):
    global _options
    _options = manifest["options"]


def init(core: VACore):
    pass


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("ё", "е").split())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def process(core: VACore, command: str, context: dict, allow_rest_phrase: bool = True):
    """-> (ключ в context, уверенность, остаток фразы) или None."""
    threshold = float(_options.get("threshold", 0.78))
    ambiguity = float(_options.get("ambiguity", 0.06))
    min_len = int(_options.get("min_key_len", 4))
    cmd = _norm(command)
    words = cmd.split()
    if not words:
        return None
    sure = float(_options.get("sure", 0.93))
    scored = []      # (уверенность, ключ, остаток, вариант, слов совпало)
    for keyall in context:
        for key in keyall.split("|"):
            k = _norm(key)
            if len(k) < min_len:
                continue
            n = len(k.split())
            if not allow_rest_phrase:
                scored.append((_ratio(cmd, k), keyall, "", key, len(words)))
                continue
            # начало фразы той же длины в словах (±1 — «включи-ка», пропущенный предлог)
            for m in (n, n - 1, n + 1):
                if 1 <= m <= len(words):
                    rest = " ".join(words[m:])
                    s = _ratio(" ".join(words[:m]), k)
                    if m != n:
                        s -= 0.03          # при прочих равных — вариант с тем же числом слов
                    scored.append((s, keyall, rest, key, m))
    if not scored:
        return None

    def rank(c):
        # среди почти точных совпадений важнее то, что покрывает больше слов фразы
        return (c[4] if c[0] >= sure else 0, c[0])
    scored.sort(key=rank, reverse=True)
    best = scored[0]
    if best[0] < threshold:
        return None
    tier = rank(best)[0]
    second = next((c for c in scored[1:] if c[1] != best[1] and rank(c)[0] == tier), None)
    if second is not None and best[0] - second[0] < ambiguity:
        logger.info(f"Нечётко: «{command}» — похоже и на «{best[3]}» ({best[0]:.2f}), "
                    f"и на «{second[3]}» ({second[0]:.2f}), не угадываю")
        return None
    logger.info(f"Нечётко: «{command}» -> «{best[3]}» ({best[0]:.2f}), остаток «{best[2]}»")
    return best[1], best[0], best[2]
