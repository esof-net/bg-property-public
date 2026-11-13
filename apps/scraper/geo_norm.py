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
    if not t or _NOT_A_PLACE.search(t):
        return None
    return t


def strip_city_prefix(area, city):
    """alo.bg: "Пловдив Център" in a Пловдив listing -> "Център". Only a leading word equal to the listing's own city is dropped, and only
    when something is left ("Пловдив" alone, or under another city such as Асеновград, stays)."""
    if not area or not city:
        return area
    a, c = str(area).strip(), str(city).strip()
    if a.casefold().startswith(c.casefold() + " ") and a[len(c):].strip():
        return a[len(c):].strip()
    return area
