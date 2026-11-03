# Режим «максимальная частота» процессора на время распознавания и озвучки.
#
# В покое процессор работает под своим обычным регулятором частоты (ondemand / schedutil / ...): холоднее и
# тише. Как только клиент прислал звук фразы или Ирина начала синтез речи, регулятор переключается на
# performance — ядра сразу выходят на максимальную частоту, не дожидаясь, пока регулятор «разгонится».
# Через hold секунд после последней активности возвращается прежний регулятор.
#
# Писать в /sys/.../scaling_governor может только root; install.sh разрешает это пользователю Ирины
# правилом /etc/tmpfiles.d/irene-cpufreq.conf (применяется при каждой загрузке).

import atexit
import glob
import logging
import os
import threading
import time

logger = logging.getLogger("rooms")

BOOST_GOVERNOR = "performance"


class CpuBoost:
    def __init__(self, enabled=True, hold=3.0):
        self.enabled = enabled
        self.hold = float(hold)
        self.files = sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor")) or \
            sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"))
        self.saved = {}               # файл -> обычный регулятор
        self.active = False
        self.until = 0.0
        self.holds = 0                # открытые «with boost:» (синтез идёт)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.ok = self._check()
        if self.ok:
            threading.Thread(target=self._loop, name="cpu-boost", daemon=True).start()
            atexit.register(self._restore)

    def _check(self):
        if not self.files:
            logger.info("Ускорение процессора: регулятор частоты недоступен (нет cpufreq) — выключено")
            return False
        for f in self.files:
            try:
                with open(f) as fh:
                    gov = fh.read().strip()
                avail = open(os.path.join(os.path.dirname(f), "scaling_available_governors")).read().split()
            except OSError as e:
                logger.info(f"Ускорение процессора: не прочитать {f}: {e}")
                return False
            if BOOST_GOVERNOR not in avail:
                logger.warning(f"Ускорение процессора: в ядре нет регулятора performance ({' '.join(avail)})")
                return False
            # Ирина могла упасть посреди ускорения: тогда «обычным» считаем то, что настроено в системе
            self.saved[f] = gov if gov != BOOST_GOVERNOR else self._system_default(avail)
            if not os.access(f, os.W_OK):
                logger.warning("Ускорение процессора: нет прав менять частоту. Повторите install.sh или выполните:\n"
                               "  echo 'z /sys/devices/system/cpu/cpufreq/policy*/scaling_governor 0664 root "
                               f"{_group()} -' | sudo tee /etc/tmpfiles.d/irene-cpufreq.conf && "
                               "sudo systemd-tmpfiles --create /etc/tmpfiles.d/irene-cpufreq.conf")
                return False
        self._write_all(self.saved)     # если остался performance от упавшего процесса — вернуть
        logger.info(f"Ускорение процессора на время речи: {', '.join(sorted(set(self.saved.values())))} -> "
                    f"{BOOST_GOVERNOR}, держать {self.hold:g} с")
        return True

    @staticmethod
    def _system_default(avail):
        try:
            for line in open("/etc/default/cpufrequtils"):
                if line.strip().startswith("GOVERNOR="):
                    g = line.split("=", 1)[1].strip().strip('"')
                    if g in avail and g != BOOST_GOVERNOR:
                        return g
        except OSError:
            pass
        for g in ("schedutil", "ondemand", "interactive", "conservative", "powersave"):
            if g in avail:
                return g
        return avail[0]

    def _write_all(self, govs):
        for f, g in govs.items():
            try:
                with open(f, "w") as fh:
                    fh.write(g)
            except OSError as e:
                logger.warning(f"Ускорение процессора: не записать {g} в {f}: {e}")

    # ---------------------------------------------------------------- для вызова из кода
    def pulse(self):
        """Активность (пришёл звук фразы): максимальная частота ещё hold секунд."""
        if not (self.ok and self.enabled):
            return
        with self.lock:
            self.until = time.monotonic() + self.hold
            if not self.active:
                self._on()

    def __enter__(self):
        if self.ok:
            with self.lock:
                self.holds += 1
                if self.enabled and not self.active:
                    self._on()
        return self

    def __exit__(self, *exc):
        if self.ok:                       # без проверки enabled: выключили посреди синтеза — счётчик всё равно сойдётся
            with self.lock:
                self.holds = max(0, self.holds - 1)
                self.until = max(self.until, time.monotonic() + self.hold)
        return False

    # ---------------------------------------------------------------- внутреннее
    def _on(self):
        self._write_all({f: BOOST_GOVERNOR for f in self.files})
        self.active = True
        self.cond.notify()

    def _loop(self):
        with self.lock:
            while True:
                if not self.active:
                    self.cond.wait()
                    continue
                left = self.until - time.monotonic()
                if self.holds == 0 and left <= 0:
                    self._write_all(self.saved)
                    self.active = False
                    continue
                self.cond.wait(timeout=max(0.2, left) if self.holds == 0 else 0.5)

    def _restore(self):
        if self.active:
            self._write_all(self.saved)
            self.active = False


def _group():
    try:
        import grp
        return grp.getgrgid(os.getgid()).gr_name
    except Exception:
        return "wint"


_instance = None


def get(options=None) -> CpuBoost:
    """Один на процесс; options — настройки сервера комнат (cpu_boost, cpu_boost_hold_sec)."""
    global _instance
    if _instance is None:
        o = options or {}
        _instance = CpuBoost(bool(o.get("cpu_boost", True)), float(o.get("cpu_boost_hold_sec", 3)))
    return _instance
