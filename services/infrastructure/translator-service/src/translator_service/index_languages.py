"""Index inventory: arXiv:2609.40181v1, appendix C, tables 31–32.

Supplier identifiers are normalized to the existing lowercase BCP-style codes.
ISO aliases are mappings, not additional model languages or quality claims.
"""

from translator_service.protocol import TranslatorConfigurationError

_CORE = {
    "en": "英语",
    "zh": "中文",
    "de": "德语",
    "fr": "法语",
    "es": "西班牙语",
    "ja": "日语",
    "ko": "韩语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "ro": "罗马尼亚语",
    "sv": "瑞典语",
    "tr": "土耳其语",
    "hi": "印地语",
    "vi": "越南语",
    "th": "泰语",
    "id": "印尼语",
    "ms": "马来语",
    "fil": "菲律宾语",
}
_ADDITIONAL = """
ukr-cyrl|Ukrainian
fas-arab|Persian
ces-latn|Czech
ell-grek|Greek
dan-latn|Danish
hun-latn|Hungarian
fin-latn|Finnish
nob-latn|Norwegian Bokmål
slk-latn|Slovak
bul-cyrl|Bulgarian
bos-latn|Bosnian
cat-latn|Catalan
ben-beng|Bengali
heb-hebr|Hebrew
lit-latn|Lithuanian
slv-latn|Slovenian
ekk-latn|Estonian
als-latn|Albanian
lvs-latn|Latvian
azj-latn|Azerbaijani
hrv-latn|Croatian
tam-taml|Tamil
npi-deva|Nepali
urd-arab|Urdu (Arabic script)
mkd-cyrl|Macedonian
srp-cyrl|Serbian (Cyrillic)
mar-deva|Marathi
kat-geor|Georgian
mal-mlym|Malayalam
kaz-cyrl|Kazakh
isl-latn|Icelandic
glg-latn|Galician
kan-knda|Kannada
ary-arab|Moroccan Arabic
guj-gujr|Gujarati
bel-cyrl|Belarusian
afr-latn|Afrikaans
hye-armn|Armenian
khk-cyrl|Mongolian
khm-khmr|Khmer
eus-latn|Basque
mya-mymr|Burmese
lat-latn|Latin
uzn-cyrl|Uzbek (Cyrillic)
tel-telu|Telugu
ory-orya|Odia
nno-latn|Norwegian Nynorsk
uzn-latn|Uzbek (Latin)
swh-latn|Swahili
sin-sinh|Sinhala
kir-cyrl|Kyrgyz
som-latn|Somali
pan-guru|Punjabi
arz-arab|Egyptian Arabic
cym-latn|Welsh
nrm-latn|Norman
pbt-arab|Pashto
gle-latn|Irish
srp-latn|Serbian (Latin)
hau-latn|Hausa
ckb-arab|Central Kurdish
mlt-latn|Maltese
yue-hani|Cantonese
tgk-cyrl|Tajik
kmr-latn|Northern Kurdish
bew-latn|Betawi
amh-ethi|Amharic
lao-laoo|Lao
ltz-latn|Luxembourgish
fry-latn|Frisian
div-thaa|Dhivehi
epo-latn|Esperanto
kin-latn|Kinyarwanda
ars-arab|Najdi Arabic
fao-latn|Faroese
plt-latn|Malagasy
asm-beng|Assamese
snd-arab|Sindhi
xho-latn|Xhosa
tuk-latn|Turkmen
hat-latn|Haitian Creole
gla-latn|Scottish Gaelic
ceb-latn|Cebuano
ydd-hebr|Yiddish
jav-latn|Javanese
bak-cyrl|Bashkir
pap-latn|Papiamento
mri-latn|Māori
uig-arab|Uyghur
tat-cyrl|Tatar
bod-tibt|Tibetan
mww-latn|White Hmong
hyw-armn|Western Armenian
yor-latn|Yoruba
zul-latn|Zulu
sdh-arab|Southern Kurdish
smo-latn|Samoan
cos-latn|Corsican
hif-latn|Fiji Hindi
sun-latn|Sundanese
nya-latn|Chichewa
ibo-latn|Igbo
haw-latn|Hawaiian
sot-latn|Sesotho
lus-latn|Mizo
sna-latn|Shona
chv-cyrl|Chuvash
azb-arab|South Azerbaijani
roh-latn|Romansh
run-latn|Kirundi
hin-latn|Hindi (Latin)
sah-cyrl|Yakut
tir-ethi|Tigrinya
ast-latn|Asturian
oci-latn|Occitan
cnh-latn|Hakha Chin
sme-latn|Northern Sami
nds-latn|Low German
oss-cyrl|Ossetian
urd-latn|Urdu (Latin)
gsw-latn|Swiss German
anp-deva|Angika
apc-arab|Levantine Arabic
gaz-latn|Oromo
nap-latn|Neapolitan
hsb-latn|Upper Sorbian
hil-latn|Hiligaynon
kal-latn|Greenlandic
"""
LANGUAGES = {
    **_CORE,
    **{
        line.split("|", 1)[0]: line.split("|", 1)[1]
        for line in _ADDITIONAL.splitlines()
        if line
    },
}
_ALIASES = {
    "eng-latn": "en",
    "zho-hans": "zh",
    "zh-hans": "zh",
    "pes-arab": "fas-arab",
    "yue-hant": "yue-hani",
    "tl": "fil",
    "uk": "ukr-cyrl",
    "fa": "fas-arab",
    "cs": "ces-latn",
    "el": "ell-grek",
    "da": "dan-latn",
    "hu": "hun-latn",
    "fi": "fin-latn",
    "nb": "nob-latn",
    "no": "nob-latn",
    "sk": "slk-latn",
    "bg": "bul-cyrl",
    "bs": "bos-latn",
    "ca": "cat-latn",
    "bn": "ben-beng",
    "he": "heb-hebr",
    "lt": "lit-latn",
    "sl": "slv-latn",
    "et": "ekk-latn",
    "sq": "als-latn",
    "lv": "lvs-latn",
    "az": "azj-latn",
    "hr": "hrv-latn",
    "ta": "tam-taml",
    "ne": "npi-deva",
    "ur": "urd-arab",
    "mk": "mkd-cyrl",
    "sr": "srp-cyrl",
    "sr-cyrl": "srp-cyrl",
    "sr-latn": "srp-latn",
    "mr": "mar-deva",
    "ka": "kat-geor",
    "ml": "mal-mlym",
    "kk": "kaz-cyrl",
    "is": "isl-latn",
    "gl": "glg-latn",
    "kn": "kan-knda",
    "gu": "guj-gujr",
    "be": "bel-cyrl",
    "af": "afr-latn",
    "hy": "hye-armn",
    "mn": "khk-cyrl",
    "km": "khm-khmr",
    "eu": "eus-latn",
    "my": "mya-mymr",
    "la": "lat-latn",
    "uz": "uzn-latn",
    "uz-latn": "uzn-latn",
    "uz-cyrl": "uzn-cyrl",
    "te": "tel-telu",
    "or": "ory-orya",
    "nn": "nno-latn",
    "sw": "swh-latn",
    "si": "sin-sinh",
    "ky": "kir-cyrl",
    "so": "som-latn",
    "pa": "pan-guru",
    "cy": "cym-latn",
    "ps": "pbt-arab",
    "ga": "gle-latn",
    "ha": "hau-latn",
    "mt": "mlt-latn",
    "tg": "tgk-cyrl",
    "ku": "kmr-latn",
    "am": "amh-ethi",
    "lo": "lao-laoo",
    "lb": "ltz-latn",
    "fy": "fry-latn",
    "dv": "div-thaa",
    "eo": "epo-latn",
    "rw": "kin-latn",
    "fo": "fao-latn",
    "mg": "plt-latn",
    "as": "asm-beng",
    "sd": "snd-arab",
    "xh": "xho-latn",
    "tk": "tuk-latn",
    "ht": "hat-latn",
    "gd": "gla-latn",
    "jv": "jav-latn",
    "ba": "bak-cyrl",
    "mi": "mri-latn",
    "ug": "uig-arab",
    "tt": "tat-cyrl",
    "bo": "bod-tibt",
    "yo": "yor-latn",
    "zu": "zul-latn",
    "sm": "smo-latn",
    "co": "cos-latn",
    "su": "sun-latn",
    "ny": "nya-latn",
    "ig": "ibo-latn",
    "st": "sot-latn",
    "sn": "sna-latn",
    "cv": "chv-cyrl",
    "rm": "roh-latn",
    "rn": "run-latn",
    "ti": "tir-ethi",
    "oc": "oci-latn",
    "se": "sme-latn",
    "os": "oss-cyrl",
    "kl": "kal-latn",
    "yi": "ydd-hebr",
}


def language_name(code: str, *, source: bool = False) -> str:
    """Resolve a known language without silently discarding an unknown script."""
    canonical = _ALIASES.get(code, code)
    if canonical not in LANGUAGES:
        base, _, region = code.rpartition("-")
        if len(region) == 2 or (len(region) == 3 and region.isdecimal()):
            canonical = _ALIASES.get(base, base)
    if canonical not in LANGUAGES:
        kind = "SOURCE" if source else "TARGET"
        raise TranslatorConfigurationError(f"TRANSLATOR_UNSUPPORTED_{kind}_LANGUAGE")
    return LANGUAGES[canonical]
