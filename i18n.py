"""
i18n.py – Mehrsprachigkeit für den NMEA2000 Viewer / multi-language support
==========================================================================

Prinzip (wie gettext): Die Texte im Quellcode sind Deutsch (Quellsprache "de").
Jede weitere Sprache ist eine JSON-Datei ``locales/<code>.json`` mit
``{"Deutscher Text": "translation", ...}``. Der Eintrag ``"__name__"`` ist der Name
der Sprache in ihrer eigenen Sprache (erscheint in der Sprachauswahl).

Neue Sprache hinzufügen
-----------------------
1. ``locales/en.json`` nach ``locales/fr.json`` kopieren.
2. ``"__name__"`` und die Werte (rechte Seite) übersetzen. Fehlende Einträge
   werden automatisch auf Deutsch angezeigt.
3. Viewer neu starten – die Sprache erscheint in der Auswahl. Kein Code nötig.

Fehlende Übersetzungen findest du mit::

    python i18n.py fr          # listet alle Texte, die in fr.json fehlen

Übersetzungen in Plugins (pgn_plugins/*.py)
-------------------------------------------
::

    from i18n import add_translations
    add_translations("en", {"Meine Firma: Sensorwerte": "My company: sensor values",
                            "Druck": "Pressure"})

Feldnamen, Aufzählungswerte und PGN-Namen laufen automatisch durch ``t()``;
Texte ohne Übersetzung bleiben unverändert (Plugins dürfen also auch direkt
englisch benannt sein).
"""
import json
import locale
import os

SOURCE = "de"
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

_cat = {}                 # code -> {Quelltext: Übersetzung}
_names = {SOURCE: "Deutsch"}
_lang = SOURCE


def load_locales(path=None):
    """Liest alle locales/*.json (kann jederzeit erneut aufgerufen werden)."""
    path = path or LOCALE_DIR
    if not os.path.isdir(path):
        return
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        code = fn[:-5]
        try:
            with open(os.path.join(path, fn), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:      # kaputte Sprachdatei darf den Viewer nicht stoppen
            print("i18n: {}: {}".format(fn, e))
            continue
        _names[code] = str(data.pop("__name__", code))
        _cat.setdefault(code, {}).update({k: v for k, v in data.items() if isinstance(v, str)})


def add_translations(lang, mapping, name=None):
    """Übersetzungen zur Laufzeit ergänzen (z. B. aus Plugins)."""
    _cat.setdefault(lang, {}).update(mapping)
    if name:
        _names[lang] = name
    _names.setdefault(lang, lang)


def languages():
    """[(code, Anzeigename), ...] – Quellsprache zuerst."""
    codes = [SOURCE] + sorted(c for c in _names if c != SOURCE)
    return [(c, _names[c]) for c in codes]


def get_language():
    return _lang


def set_language(code):
    global _lang
    _lang = code if code in _names else SOURCE
    return _lang


def detect_language():
    """Systemsprache, falls verfügbar – sonst Englisch (falls vorhanden), sonst Deutsch."""
    import re
    import warnings
    names = []
    with warnings.catch_warnings():             # getdefaultlocale() ist in neueren Python-Versionen "deprecated"
        warnings.simplefilter("ignore")
        for getter in (locale.getdefaultlocale, locale.getlocale):     # liefert z. B. "de_CH" (Windows: auch "German_Switzerland")
            try:
                names.append(getter()[0] or "")
            except Exception:
                pass
    for name in names:
        m = re.match(r"([A-Za-z]{2})(?:[_\-.@]|$)", name)
        if m and m.group(1).lower() in _names:
            return m.group(1).lower()
    return "en" if "en" in _names else SOURCE


def t(text):
    """Text in die aktuelle Sprache übersetzen; unbekannte Texte bleiben unverändert."""
    if _lang == SOURCE or not isinstance(text, str):
        return text
    return _cat.get(_lang, {}).get(text, text)


def missing(code):
    """Quelltexte aus de→en-Datei, die in <code> noch fehlen (Hilfe für Übersetzer)."""
    ref = _cat.get("en", {})
    have = _cat.get(code, {})
    return [k for k in ref if k not in have]


load_locales()

if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "en"
    for k in missing(code):
        print(k)
