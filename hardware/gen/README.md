# Генератор платы клиента

Плата описана кодом — так схема, перечень, разводка и Gerber всегда согласованы между собой.

| Файл | Что внутри |
|---|---|
| `design.py` | детали, посадочные места, выводы → цепи, описания для перечня |
| `placement.py` | размер платы, расстановка деталей, подписи на шелкографии |
| `board.py` | загрузка посадочных мест из библиотек KiCad (через kiutils), проверка перекрытий |
| `route.py` | выгрузка в Specctra DSN для Freerouting, чтение результата (SES), выводы земли на нижний слой |
| `gerber.py` | заливка земли, Gerber RS-274X, сверловка Excellon, шрифт шелкографии |
| `verify.py` | независимая проверка готовых Gerber: связность цепей и отсутствие замыканий |
| `schematic.py` | принципиальная схема (SVG → PDF) |
| `render.py` | картинки разводки и сборочного чертежа |
| `build.py` | всё вместе → `../irene-client` |
| `routed.ses` | сохранённая разводка (Freerouting 1.9) |

Нужно: Python 3 с numpy, scipy, Pillow, playwright (для PDF схемы); папка `IRENE_PCB_EXT` с
[kiutils](https://github.com/mvnmgrx/kiutils) (`kiutils/src`), библиотеками посадочных мест KiCad
(`kicad-footprints`, нужные `.pretty`) и Espressif (`kicad-libraries`), для `--route` ещё `freerouting19.jar`, Java и xvfb.

Если двигаете детали в `placement.py`, разводку нужно повторить: `python3 build.py --route` (5–15 минут).
