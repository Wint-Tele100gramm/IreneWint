# Playwav plugin for aplay program
# source: https://github.com/mobez/Irene-Voice-Assistant/blob/master/plugins/plugin_playwav_aplay.py

import os

from vacore import VACore
#from aplay import Aplay
import subprocess

modname = os.path.basename(__file__)[:-3] # calculating modname

# функция на старте
def start(core:VACore):
    manifest = {
        "name": "PlayWav through aplay",
        "version": "1.0",
        "require_online": False,

        "playwav": {
            "aplay": (init,playwav) # первая функция инициализации, вторая - проиграть wav-файл
        }
    }
    return manifest

def start_with_options(core:VACore, manifest:dict):
    pass

def init(core:VACore):
    pass

def playwav(core:VACore, wavfile:str):
    # список аргументов, а не строка через shell: в именах файлов кэша озвучки есть пробелы и "!"
    # процесс сохраняем в core, чтобы ответ можно было прервать голосом ("стоп") — см. stop_playback()
    proc = subprocess.Popen(["aplay", "-q", wavfile])
    core.current_playback = proc
    try:
        proc.wait()
    finally:
        core.current_playback = None


def stop_playback(core:VACore):
    proc = getattr(core, "current_playback", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        return True
    return False
    #AudioPlayer(wavfile).play(block=True)
    return

