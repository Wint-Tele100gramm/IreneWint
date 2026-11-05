# Генератор платы клиента

Плата описана кодом — так схема, перечень, разводка и Gerber всегда согласованы между собой.

| Файл | Что внутри |
|---|---|
| `design.py` | детали, посадочные места, выводы → цепи, описания для перечня |
| `placement.py` | размер платы, расстановка деталей, подписи на шелкографии |
| `board.py` | загрузка посадочных мест из библиотек KiCad (через kiutils), проверка перекрытий |
| `route.py` | выгрузка в Specctra DSN для Freerouting, чтение результата (SES), выводы земли на нижний слой |
| `finish.py` | дотрассировка связей, которые не довёл Freerouting (волновой трассировщик A* с теми же зазорами) |
| `kicad_pcb.py`, `kicad_sch.py` | проект KiCad: плата (с разводкой и заливками), схема, правила |
| `pcb_check.py`, `sch_check.py` | проверка файлов KiCad: площадки и цепи платы, цепи схемы — против `design.py` |
| `gerber.py` | заливка земли (векторные многоугольники), Gerber RS-274X, сверловка Excellon, шрифт шелкографии |
| `lay6.py`, `lay6_export.py`, `lay6_view.py` | Sprint Layout 6: чтение/запись формата, выгрузка платы, картинка файла для проверки |
| `verify.py` | независимая проверка готовых Gerber: связность цепей и отсутствие замыканий |
| `schematic.py` | принципиальная схема (SVG → PDF) |
| `render.py` | картинки разводки и сборочного чертежа |
| `build.py` | всё вместе → `../irene-client` |
| `routed.ses` | сохранённая разводка (Freerouting 1.9) |

Нужно: Python 3 с numpy, scipy, Pillow, playwright (для PDF схемы); папка `IRENE_PCB_EXT` с
[kiutils](https://github.com/mvnmgrx/kiutils) (`kiutils/src`), библиотеками посадочных мест KiCad
(`kicad-footprints`, нужные `.pretty`) и Espressif (`kicad-libraries`), для `--route` ещё `freerouting19.jar`, Java и xvfb.

Если двигаете детали в `placement.py`, разводку нужно повторить: `python3 build.py --route` (5–15 минут).
Дальше плату удобнее дорабатывать прямо в KiCad (`../irene-client/kicad`) — генератор нужен для первой версии.
