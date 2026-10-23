# Перенесено из форка Ирины от AlexeyBond (https://github.com/AlexeyBond/Irene-Voice-Assistant, MIT).
from utils.ru_speech.gender import FEMALE, MALE
from utils.ru_speech.word_forms import FullKnownFormsRU, KnownFormsRU

SECOND = FullKnownFormsRU(
    singular=KnownFormsRU("секунда", "секунды", "секунде", "секунду", "секундой", "секунде"),
    plural=KnownFormsRU("секунды", "секунд", "секундам", "секунды", "секундами", "секундах"),
    gender=FEMALE.code,
)

MINUTE = FullKnownFormsRU(
    singular=KnownFormsRU("минута", "минуты", "минуте", "минуту", "минутой", "минуте"),
    plural=KnownFormsRU("минуты", "минут", "минутам", "минуты", "минутами", "минутах"),
    gender=FEMALE.code,
)

HOUR = FullKnownFormsRU(
    singular=KnownFormsRU("час", "часа", "часу", "час", "часом", "часе"),
    plural=KnownFormsRU("часы", "часов", "часам", "часы", "часами", "часах"),
    gender=MALE.code,
)
