"""Alias normalisation shared by the geo seed tools and the resolver.

Deliberately gentle: case-fold, punctuation and underscores/hyphens to single spaces. It does NOT drop letters (the older
normalize_area() folds ъ to a, which merges distinct names) and does NOT strip words such as "област" ("София област" is a
different oblast from "София"); prefixes like "гр." / "с." are handled by the resolver, which knows the context.
"""
import re
import unicodedata

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def norm_alias(text):
    if text is None:
        return ""
    return _NON_WORD.sub(" ", unicodedata.normalize("NFC", str(text)).casefold()).strip()


_EDGE_JUNK = re.compile(r"^[\s,.\-|]+|[\s,\-|]+$")  # a trailing "." is kept ("20 april str.")
# Cyrillic only: a Latin one-letter word is a Roman numeral in imoti.net ("V Primery school" = 5th primary school)
_LEADING_LETTER = re.compile(r"^[А-Яа-я]\s+(?=\S)")
# alo.bg floor text glued in front of the place ("Непоследен Изгрев") and a compass tag in front of a real name ("Изток Център");
# "Запад 2" (a numbered quarter) is left alone: the tag only goes when a non-digit name follows
_LEADING_NOISE = re.compile(r"^(?:Непоследен|Изток|Запад|Север|Юг)\s+(?=[^\d\s])")
# imoti.net card tails that are not a place: "60 м 2 Varna" (size + city), "7 - 11 km" (distance), "2 bedroom apartment, ..."
_NOT_A_PLACE = re.compile(r"\b\d+\s*м\s?2\b|^\d+(?:\s*-\s*\d+)?\s*km\b|\b(?:bedroom|bedsit|apartment)\b", re.IGNORECASE)
# An ad headline that was taken for the area (bazar.bg / olx.bg "2-ен апартамент, район РЕЖИЯТА– 58000евро!"): a price or a "!" is never in a place name
_AD_TEXT = re.compile(r"!|\d[\d\s.,]*\s*(?:евро|лв|€|eur)\b|\w-(?:едно|дву|три|четири|много)стаен", re.IGNORECASE)
# imot.bg villages ("Абланица, област Ловеч") and bcpea ("Сунгурларе, град Сунгурларе, община Сунгурларе, област Бургас"): the settlement is
# the part in front of the first administrative qualifier
_ADMIN_TAIL = re.compile(r",\s*(?:област|община|град)\s", re.IGNORECASE)
# alo.bg glues the ad title in front of the place ("Двустаен Апартамент Възрожденци", "Партер Възрожденци", "Продавам Апартамент Меден Рудник").
# Words that describe the offer, never a place; only dropped from the FRONT, and only while something is left
_NOISE_WORDS = frozenset(w for w in """
апартамент апартамет апартаменти апартаменте едностаен двустаен тристаен четиристаен многостаен стаен двустаяен тристаяен тристайни
гарсониера мезонет студио партер последен непоследен продава продавам обзаведен тухла панел гараж паркомясто собственик евро
""".split())
# "Нов" is also the start of real names ("Нов Център"), so it goes only when an offer word follows ("Нов Апартамент Кършияка")
_NEW_WORD = "нов"
# "2-стаен", "3-стайни" (one token) and "3 стаен" (digit token + room word); "2-ен" (the same, shortened) only goes in front of another offer word
# ("2-ен апартамент Център"). Ordinals of real names ("2-ра гара", "3-ти квартал") do not match.
_ROOM_COUNT_TOKEN = re.compile(r"^\d+-?(?:стаен|стаена|стайн\w*)$")
_ROOM_WORD = re.compile(r"^(?:стаен|стаена|стайн\w*)$")
_ROOM_COUNT_SHORT = re.compile(r"^\d+-ен$")
_WORD_SPLIT = re.compile(r"\s+")


def clean_area(text):
    """The one place a scraped `area` string is cleaned (scrapers, sync, resolver and the quarter builder all call this).

    Removes what the portals' text cutting leaves behind and is not part of the place: surrounding punctuation (homes.bg ", Варна"),
    one-letter words in front (alo.bg "С Възраждане" from "БУРГАС Възраждане", "У Център" from "СУ Център"), and text that is
    not a place at all (imoti.net "60 м 2 Varna", "7 - 11 km"). Returns None when nothing is left. Real names are left alone."""
    if text is None:
        return None
    if str(text).lstrip().startswith(","):
        return None  # ", Варна": the part in front of the comma (the area) is empty; what follows is the city
    t = _EDGE_JUNK.sub("", str(text)).strip()
    while _LEADING_LETTER.match(t) or _LEADING_NOISE.match(t):
        t = _LEADING_NOISE.sub("", _LEADING_LETTER.sub("", t, count=1), count=1).strip()
    t = _EDGE_JUNK.sub("", t).strip()
    if not t or _NOT_A_PLACE.search(t) or _AD_TEXT.search(t):
        return None
    if _ADMIN_TAIL.search(t):
        t = _ADMIN_TAIL.split(t, 1)[0].strip()
    t = _drop_noise_words(t)
    if not t:
        return None
    return t


def area_or_city(area, city):
    """What a scraper stores as the listing's area: the cleaned area, or the city when nothing place-like is left (an ad headline, a price)."""
    return clean_area(area) or city


def _drop_noise_words(t):
    """Drops the offer words glued in front of a place; None-like "" when nothing but offer words was there."""
    words = _WORD_SPLIT.split(t) if t else []
    i = 0
    while i < len(words):
        w = words[i].casefold().strip(",.")
        nxt = words[i + 1].casefold().strip(",.") if i + 1 < len(words) else ""
        if (w in _NOISE_WORDS or _ROOM_COUNT_TOKEN.match(w)
                or (w == _NEW_WORD and nxt in _NOISE_WORDS)
                or (w.isdigit() and _ROOM_WORD.match(nxt))
                or (_ROOM_COUNT_SHORT.match(w) and nxt in _NOISE_WORDS)):
            i += 1
        else:
            break
    return " ".join(words[i:])


def snap_to_known_area(area, known_keys, key_fn):
    """Free-text portals (alo.bg, bazar.bg, olx.bg) glue shop names, streets and ad words in front of the place ("Билла Възрожденци",
    "Ардагаз Възрожденци"). `known_keys` are the area keys the structured portals (imot.bg, homes.bg, imoti.bg: a dedicated location field)
    gave for the same city. When the area is not one of them, the longest run of trailing words that is one wins; otherwise the area is
    kept as it is. At least one word is always left, so the result is never empty."""
    if not area or not known_keys:
        return area
    if key_fn(area) in known_keys:
        return area
    words = _WORD_SPLIT.split(str(area).strip())
    for start in range(1, len(words)):
        tail = " ".join(words[start:])
        if key_fn(tail) in known_keys:
            return tail
    return area


def strip_city_prefix(area, city):
    """alo.bg: "Пловдив Център" in a Пловдив listing -> "Център". Only a leading word equal to the listing's own city is dropped, and only
    when something is left ("Пловдив" alone, or under another city such as Асеновград, stays)."""
    if not area or not city:
        return area
    a, c = str(area).strip(), str(city).strip()
    if a.casefold().startswith(c.casefold() + " ") and a[len(c):].strip():
        return a[len(c):].strip()
    return area
