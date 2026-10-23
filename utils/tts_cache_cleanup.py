# Очистка кэша озвучки (идея из форка AlexeyBond): кэш фраз растёт без конца, а eMMC у приставки маленькая.
# Время изменения файла = время последнего использования фразы (ядро обновляет его при каждом попадании в кэш).

import os
import time


def cleanup(cache_dir: str, max_mb: float = 0, max_days: float = 0, min_age_sec: float = 60):
    """Удаляет фразы, не звучавшие дольше max_days, и самые давние сверх max_mb.
    0 — без ограничения. Возвращает (сколько удалено, сколько байт освобождено)."""
    if not os.path.isdir(cache_dir):
        return 0, 0
    files = []
    for root, _, names in os.walk(cache_dir):
        for name in names:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
    files.sort(reverse=True)             # сначала недавно звучавшие

    now = time.time()
    keep_bytes = (max_mb or 0) * 1024 * 1024
    oldest = now - max_days * 86400 if max_days and max_days > 0 else None
    total, deleted, freed = 0, 0, 0
    for mtime, size, path in files:
        if now - mtime < min_age_sec:    # только что записанный файл не трогаем
            total += size
            continue
        too_old = oldest is not None and mtime < oldest
        too_much = keep_bytes > 0 and total + size > keep_bytes
        if too_old or too_much:
            try:
                os.unlink(path)
                deleted += 1
                freed += size
            except OSError:
                pass
        else:
            total += size
    return deleted, freed
