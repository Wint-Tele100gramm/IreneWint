# Числа в тексте -> слова для озвучки: «-30.5°, 120-130, 15%» -> «минус тридцать целых пять десятых ...».
# Раньше использовалась библиотека lingua_franca (229 файлов); здесь — на num_to_text_ru, без зависимостей.

import re

from utils.num_to_text_ru import num2text

_FRACTION_UNITS = {
    1: (("десятая", "десятых", "десятых"), "f"),
    2: (("сотая", "сотых", "сотых"), "f"),
    3: (("тысячная", "тысячных", "тысячных"), "f"),
}
_WHOLE = (("целая", "целых", "целых"), "f")


def number_to_words(token: str) -> str:
    """«42» -> «сорок два», «-3.5» / «3,5» -> «минус три целых пять десятых»."""
    token = token.replace(",", ".")
    negative = token.startswith("-")
    token = token.lstrip("+-")
    if "." in token:
        whole, frac = token.split(".", 1)
        frac = frac[:3].rstrip("0")
        if frac:
            text = f"{num2text(int(whole or 0), _WHOLE)} {num2text(int(frac), _FRACTION_UNITS[len(frac)])}"
        else:
            text = num2text(int(whole or 0))
    else:
        text = num2text(int(token))
    if text.startswith("одна тысяча"):
        text = text[5:]                 # «тысяча девятьсот», а не «одна тысяча девятьсот»
    return ("минус " + text) if negative else text


def all_num_to_text(text: str) -> str:
    # диапазоны: «120-130» -> «сто двадцать тире сто тридцать»
    text = re.sub(r"(\d)\s*-\s*(\d)", r"\1 тире \2", text)
    # минус только перед числом, стоящим отдельно: «-30», но не «ID-30»
    text = re.sub(r"(?<![\w.,])-?\d+(?:[.,]\d+)?", lambda m: " " + number_to_words(m.group()) + " ", text)
    text = text.replace("%", " процентов").replace("°", " градусов")
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


if __name__ == "__main__":
    print(all_num_to_text("Ба ва 120.1-120.8, Да -30.1, Ка 44.05, Га 225. Рынок -10%. 3,5 км, 2026 год"))
