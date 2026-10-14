# Ирина выбирает команду по первому совпавшему префиксу, поэтому общий префикс ("включи", "поставь")
# может перехватить более точную команду другого плагина ("включи блютуз").
# delegate_longer_command() находит самый длинный подходящий ключ среди всех команд и, если он
# принадлежит другому обработчику, передаёт фразу туда.


def _func_of(handler):
    return handler[0] if isinstance(handler, tuple) else handler


def delegate_longer_command(core, own_module: str) -> bool:
    full = (getattr(core, "input_cmd_full", "") or "").strip()
    if not full or not isinstance(getattr(core, "commands", None), dict):
        return False
    best_key, best_handler = None, None
    for keyall, handler in core.commands.items():
        if isinstance(handler, dict):
            continue
        for key in keyall.split("|"):
            if (full == key or full.startswith(key + " ")) and (best_key is None or len(key) > len(best_key)):
                best_key, best_handler = key, handler
    if best_handler is None or getattr(_func_of(best_handler), "__module__", "") == own_module:
        return False
    core.call_ext_func_phrase(full[len(best_key):].strip(), best_handler)
    return True
