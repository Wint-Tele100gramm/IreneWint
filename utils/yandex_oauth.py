# Вход в Яндекс Музыку по коду (OAuth Device Flow) с запросом только нужных прав.
#
# API Музыки закрытый, поэтому вход идёт от имени официального приложения «Яндекс.Музыка»
# (тот же client_id, что у библиотеки yandex-music). Без параметра scope Яндекс выдаёт токен со всеми
# правами этого приложения (почта, платежи, Диск, умный дом…). Здесь права перечисляются явно.
# Документация: https://yandex.ru/dev/id/doc/ru/codes/screen-code-oauth

import secrets
import time

import requests

CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"        # приложение «Яндекс.Музыка» (Android)
CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"
OAUTH = "https://oauth.yandex.ru"

MUSIC_SCOPES = ["music:content", "music:read", "music:write"]   # каталог, чтение и изменение плейлистов/лайков

MODES = {
    # режим: (обязательные права, необязательные — пользователь сам решает на странице Яндекса)
    "music": (MUSIC_SCOPES, []),
    "music_login": (MUSIC_SCOPES, ["login:info"]),
    "all": ([], []),                    # все права приложения — как раньше
}
MODE_TITLES = {
    "music": "только Музыка",
    "music_login": "Музыка + имя аккаунта (по желанию)",
    "all": "все права приложения",
}


class OAuthError(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def _post(path, data, timeout=15):
    r = requests.post(OAUTH + path, data=data, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        raise OAuthError(body.get("error", str(r.status_code)), body.get("error_description") or r.text[:200])
    return body


def request_code(mode="music", device_name="Ирина — голосовой помощник", device_id=None):
    """Возвращает dict: device_code, user_code, verification_url, interval, expires_in."""
    scope, optional = MODES[mode]
    data = {"client_id": CLIENT_ID, "device_id": device_id or secrets.token_hex(8), "device_name": device_name}
    if scope:
        data["scope"] = " ".join(scope)
    if optional:
        data["optional_scope"] = " ".join(optional)
    return _post("/device/code", data)


def wait_token(code, should_cancel=None):
    """Ждёт, пока пользователь подтвердит вход; возвращает dict с access_token (и scope, если Яндекс его вернул)."""
    deadline = time.monotonic() + int(code.get("expires_in", 300))
    interval = max(1, int(code.get("interval", 5)))
    while time.monotonic() < deadline:
        if should_cancel and should_cancel():
            raise OAuthError("cancelled", "вход отменён")
        try:
            return _post("/token", {"grant_type": "device_code", "code": code["device_code"],
                                    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        except OAuthError as e:
            if e.code != "authorization_pending":
                raise
        time.sleep(interval)
    raise OAuthError("expired_token", "время на подтверждение истекло")


def check_token(token):
    """Проверяет, что с токеном работает Музыка. Возвращает (ok, текст)."""
    try:
        from yandex_music import Client
        client = Client(token).init()
        name = getattr(getattr(client, "me", None), "account", None)
        login = getattr(name, "login", None) or getattr(name, "display_name", None)
        plus = getattr(getattr(client.me, "plus", None), "has_plus", None)
        if plus is False:
            return True, "вход выполнен, но у аккаунта нет подписки Плюс — музыка будет фрагментами"
        return True, f"вход выполнен{f' ({login})' if login else ''}"
    except Exception as e:
        return False, f"Музыка не приняла токен: {e}"
