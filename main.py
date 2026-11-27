import calendar
import json
import os
import re
import subprocess
import sys
import time
from html import escape, unescape
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import trafilatura
from lxml import html as lxml_html
from openai import OpenAI

# ---------- Налаштування ----------
FEEDS = [
    "https://www.pravda.com.ua/rss/view_news/",
    "https://epravda.com.ua/rss/news/",
    "https://nv.ua/ukr/rss/2371.xml",
    "https://nv.ua/rss/154.xml",
    # Щоб додати ще джерело, додайте новий рядок з RSS-посиланням
]

# У яку категорію WordPress публікувати новини з певної стрічки.
# Стрічки, яких тут немає, публікуються без вказаної категорії (як раніше).
FEED_CATEGORIES = {
    "https://nv.ua/ukr/rss/2371.xml": "СПОРТ",
    "https://nv.ua/rss/154.xml": "АВТО",
}

# Позначка на сайті (rn_news_badges_options у Snippets) для категорій:
# назва категорії WordPress (великими літерами) -> ключ позначки
CATEGORY_BADGES = {
    "СПОРТ": "sport",
    "АВТО": "auto",
}
# Мета-поле, у якому Snippet «Позначки новини» зберігає плашки (СПИСОК ключів)
BADGE_META_KEY = os.environ.get("BADGE_META_KEY", "_rn_news_badges")

# Якщо в новині є відео YouTube: додатково категорії НОВИНИ та ВІДЕО і плашка 'video'
VIDEO_CATEGORIES = ["НОВИНИ", "ВІДЕО"]
VIDEO_BADGE = "video"

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

POST_STATUS = os.environ.get("POST_STATUS", "publish")  # publish або draft
MAX_POSTS = int(os.environ.get("MAX_POSTS", "10"))     # скільки новин за одну перевірку
# розділи сайту, які бот не публікує (частини адреси через кому):
# /columns/ і /opinion/ = авторські колонки та думки, /articles/ = проєкти та великі матеріали
SKIP_URL_PARTS = [
    x.strip().lower()
    for x in (os.environ.get("SKIP_URL_PARTS") or "/columns/,/articles/,/opinion/").split(",")
    if x.strip()
]
# сайти, на які бот не робить посилань і назви яких прибирає з тексту:
# завжди заблоковані сайти + те, що додано через змінну середовища
_BASE_BLOCK = {"pravda.com.ua", "eurointegration.com.ua", "epravda.com.ua", "nv.ua"}
BLOCK_LINK_HOSTS = sorted(
    _BASE_BLOCK
    | {
        x.strip().lower().removeprefix("www.")
        for x in (os.environ.get("BLOCK_LINK_HOSTS") or "").split(",")
        if x.strip()
    }
)
MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "6") or 0)  # старіші новини не публікуємо (0 = без обмеження)
USE_SOURCE_DATE = os.environ.get("USE_SOURCE_DATE", "").strip().lower() == "true"  # ставити час публікації як у джерела
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "60")) # скільки останніх новин читати з кожної стрічки
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "1200"))  # після цього нові новини не беруться
MIN_QUOTE_LEN = int(os.environ.get("MIN_QUOTE_LEN", "15"))  # коротші фрази в лапках не вважаємо цитатами
RUN_LOOP_MINUTES = int(os.environ.get("RUN_LOOP_MINUTES", "0") or 0)  # >0: працювати безперервно стільки хвилин
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "240") or 240)       # пауза між перевірками в режимі циклу
MARK_SEEN_ONLY = os.environ.get("MARK_SEEN_ONLY", "").strip().lower() == "true"  # лише запам'ятати поточні новини
MIN_TEXT = 40                                          # мінімальна довжина тексту новини
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "4"))    # скільки додаткових фото з новини
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "2") or 0)  # скільки відео YouTube вставляти в статтю
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SEEN_FILE = "seen.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}
FEED_REPORT = []  # (адреса стрічки, статус, кількість новин): для підсумку запуску

client = OpenAI(api_key=OPENAI_API_KEY)
auth = (WP_USER, WP_APP_PASSWORD)

PROMPT = """Ти досвідчений редактор українського новинного сайту.
Твоє завдання: переписати новину так, щоб вона була оригінальним текстом,
але повністю точною за фактами.

ЖОРСТКІ ПРАВИЛА:
1. Факти: не додавай нічого від себе, не вигадуй, не роби припущень і висновків,
   яких немає в оригіналі. Якщо чогось немає в тексті, цього немає і в новині.
2. Власні назви: імена, прізвища, посади, назви країн, міст, організацій,
   партій, компаній, військових частин, документів і законів залишай точно
   так, як в оригіналі (з тими самими відмінками, написанням і транслітерацією).
   Не скорочуй і не «покращуй» їх.
3. Цифри, дати, суми, відсотки, час: переноси без змін.
4. Прямі цитати в оригіналі замінені маркерами [[Q1]], [[Q2]] тощо.
   Кожен маркер ОБОВ'ЯЗКОВО має бути в тексті новини рівно в такому вигляді,
   без змін (лапки вже входять у маркер, власних лапок навколо не додавай).
   Поруч із маркером вкажи, хто це сказав. Саму цитату не пиши і не
   перефразовуй, лише маркер. Непрямі висловлювання перефразовуй вільно.
5. Не змінюй суть: не посилюй і не пом'якшуй твердження, не перетворюй
   «заявив» на «підтвердив» чи «вважає» на «стверджує».
6. Стиль: нейтральний, діловий, без епітетів, оцінок і клікбейту.
   Перша фраза відповідає на питання «що сталося?». Найважливіше на початку.
7. Структура: змінюй порядок подачі й формулювання, не копіюй речення
   з оригіналу (крім цитат). Обсяг орієнтовно такий самий, як в оригіналі.
   Якщо оригінал короткий, не розтягуй його і нічого не додавай:
   кількість абзаців від 1 до 6 залежно від обсягу.
8. Прибери з тексту рекламу, заклики підписатися, фрази на кшталт
   «читайте також» та підписи фотографів.
9. Позначки (хештеги): визнач 3 найважливіші позначки за змістом новини.
   Це ключові особи (прізвище), місця (місто, країна), організації або
   головна тема. Кожна позначка: одне слово (максимум два), у називному
   відмінку, великими літерами, без #. Приклад: ["КИЇВ", "КОРУПЦІЯ", "ЗЕЛЕНСЬКИЙ"].
10. Джерела інформації: обов'язково зберігай, хто повідомив чи заявив
    (наприклад «Про це пише ERR», «Повідомляє ДСНС», «Заявив Президент
    на своїй сторінці у Facebook»). Якщо слово або назва в оригіналі є
    посиланням, збережи його в тексті як HTML:
    <a href="адреса" target="_blank" rel="noopener">назва</a>
    на тому самому слові, з тією самою адресою. Адреси не змінюй і не
    вигадуй. Посилань, яких немає в оригіналі, не додавай.
11. Видання-джерело: не згадуй у тексті видання, з якого взято новину
    («Українська правда», «Європейська правда» / Eurointegration,
    «Економічна правда», NV / НВ (nv.ua) тощо), зокрема у фразах
    «інформує/повідомляє/пише ...»,
    і не роби посилань на сайти цих видань. Першоджерело
    (хто саме заявив чи написав: Зеленський на своєму акаунті, Bloomberg,
    ДСНС тощо) залишай, разом із посиланням на нього. Приклади:
    «Про це написав Зеленський на своєму акаунті в Х, інформує
    „Європейська правда“.» -> «Про це написав Зеленський на своєму акаунті в Х.»
    «Про це повідомляє Європейська правда з посиланням на Bloomberg.»
    -> «Про це повідомляє Bloomberg.»

12. СТРУКТУРА СТАТТІ (завжди саме так):
    Абзац 1: головна цитата новини, тобто найважливіша пряма мова (використай її
    маркер [[Qn]]) із вказівкою, хто це сказав. Якщо прямої мови в оригіналі
    немає, абзац 1 це головний факт новини однією-двома фразами.
    Абзац 2: джерело інформації, хто повідомляє: розгорнуте речення з посиланнями
    на джерела з оригіналу (телеграм-канали, сторінки посадовців у Facebook чи Х,
    видання, відомства). Приклади:
    «Про це повідомив голова Дніпропетровської ОВА <a href="...">Олександр Ганжа</a>.»
    «Про це йдеться в статистиці валютних інтервенцій <a href="...">НБУ</a>.»
    «За інформацією <a href="...">поліція</a> та <a href="...">Донецька обласна
    прокуратура</a>.»  «Про це повідомляє <a href="...">Ceske Noviny</a>.»
    Якщо посилань немає, але є згадка («Повідомляє ДСНС»), напиши її тут так само
    розгорнуто. ЗАБОРОНЕНО писати лише «Посилання: ...» або «Джерело: ...» без
    підмета й дієслова. Рядки «Посилання: ...» з оригіналу не копіюй, а
    переформулюй у речення за цим зразком.
    Далі: решта подій і деталей новини.
    Абзац із джерелом ЗАВЖДИ другий. НІКОЛИ не став його останнім і не став
    після інших деталей. Це стосується й таких речень:
    «Про це повідомляє <a href="...">Sun Boxing</a> на YouTube.»
    «Про це повідомляє <a href="...">dpa</a>.»
    «Про це пише <a href="...">Reuters</a>.»
    «Про це свідчить <a href="...">дослідження</a> компанії Gradus.»

13. ДЖЕРЕЛА В СОЦМЕРЕЖАХ: якщо посилання веде в Telegram, Facebook, X (Twitter),
    Instagram, YouTube, TikTok чи іншу мережу, у реченні про джерело ОБОВ'ЯЗКОВО
    вкажи: (а) хто саме повідомляє (прізвище й посада, назва відомства, каналу чи
    видання) і (б) в якій мережі. Посилання став на назву того, хто повідомляє, а
    НЕ на слова «повідомив», «написав», «заявив», «тут». Хто саме, бери з абзацу
    в оригіналі, а якщо там не сказано, то з назви акаунта в адресі.
    Приклади:
    «Про це повідомила <a href="...">Київська міська військова адміністрація</a> у Telegram.»
    «Про це написав <a href="...">Віталій Кім</a> у Telegram.»
    «Про це повідомив голова ОВА <a href="...">Олександр Ганжа</a> у Facebook.»
    ЗАБОРОНЕНО: «За інформацією повідомив та написав.» і будь-які речення, де
    посилання стоїть лише на дієслові. Якщо кілька посилань ведуть на один акаунт,
    назви його один раз, а решту дописів познач словами «допис 2», «допис 3».

Поверни JSON з полями:
- "title": новий інформативний заголовок до 90 символів, без клікбейту і
  без лапок-ялинок на початку,
- "content": текст новини у HTML (кожен абзац у <p>),
- "excerpt": короткий опис в 1-2 речення,
- "tags": список із 3 позначок, великими літерами.
"""


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-2000:], f, ensure_ascii=False, indent=1)


class SkipArticle(Exception):
    """Матеріал із розділу, який не публікуємо (колонки, проєкти)."""


def is_skipped_url(u):
    path = urlparse(u or "").path.lower()
    return any(part in path for part in SKIP_URL_PARTS)


def canonical_urls(html_text):
    """Канонічна адреса сторінки (link rel=canonical, og:url)."""
    try:
        doc = lxml_html.fromstring(html_text)
        return doc.xpath('//link[@rel="canonical"]/@href') + doc.xpath('//meta[@property="og:url"]/@content')
    except Exception:
        return []


def entry_urls(entry):
    """Усі адреси новини з RSS-запису (посилання, guid, додаткові посилання)."""
    urls = [entry.get("link"), entry.get("id"), entry.get("guid")]
    for l in entry.get("links", []) or []:
        if isinstance(l, dict):
            urls.append(l.get("href"))
    return [u for u in urls if isinstance(u, str) and u]


def get_article(url):
    """Повертає (текст, посилання на картинку, HTML сторінки)."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()  # 403/503 тощо: сайт блокує бота, тоді беремо текст з RSS
    if is_skipped_url(getattr(r, "url", "")):
        raise SkipArticle(r.url)  # після перенаправлення потрапили в розділ колонок/проєктів
    html = r.text
    for u in canonical_urls(html):
        if is_skipped_url(u):
            raise SkipArticle(u)
    text = trafilatura.extract(html) or ""
    meta = trafilatura.extract_metadata(html)
    image = meta.image if meta and meta.image else None
    return text, image, html


# ---------- Повні підписи для sport.nv.ua ----------
# Для цих джерел підпис до фото беремо повністю: «Опис (Фото: джерело)»
FULL_CAPTION_HOSTS = ["sport.nv.ua"] + [
    x.strip().lower().removeprefix("www.")
    for x in (os.environ.get("FULL_CAPTION_HOSTS") or "").split(",")
    if x.strip()
]
FULL_CAPTION_FEEDS = {"https://nv.ua/ukr/rss/2371.xml"}  # спортивна стрічка NV
_FULL = {"on": False}  # вмикається на час обробки однієї новини


def _lim(normal, full):
    """Ліміт довжини підпису: ширший для джерел із повними підписами."""
    return full if _FULL["on"] else normal


def is_full_caption_source(link, feed=None):
    h = urlparse(link or "").netloc.lower().removeprefix("www.")
    return feed in FULL_CAPTION_FEEDS or any(h == x or h.endswith("." + x) for x in FULL_CAPTION_HOSTS)


CREDIT_TAIL_RE = re.compile(
    r"^(?P<desc>.*?\S)\s*[.,;:—–-]?\s*"
    r"(?P<credit>(?:Ілюстративне\s+)?Фото\s*:\s*[^()]{2,150}?)\s*\.?$",
    re.I,
)


def normalize_photo_caption(c):
    """«Опис. Фото: джерело» -> «Опис (Фото: джерело)». Підпис уже у вигляді
    «Опис (Фото: ...)» або лише «Фото: ...» не змінюється."""
    c = _clean(c)
    if not c or ("(" in c and ")" in c):
        return c
    m = CREDIT_TAIL_RE.match(c)
    if not m:
        return c
    desc = m.group("desc").rstrip(" .,;:—–-")
    return f"{desc} ({m.group('credit').strip()})" if desc else c


def pick_full_caption(page_c, rss_c):
    """Найповніший підпис: той, де є вказівка «Фото: ...», а серед таких найдовший."""
    cands = [(c, src) for c, src in ((page_c, "сторінка новини"), (rss_c, "RSS")) if c]
    if not cands:
        return None, "RSS"
    def score(item):
        c = item[0]
        return (bool(re.search(r"\(?\s*(?:ілюстративне\s+)?фото\s*:", c, re.I)), len(c))
    c, src = max(cands, key=score)
    return normalize_photo_caption(c), src


# ---------- Підпис до фото (як у новині-першоджерелі) ----------
CAPTION_HINT = re.compile(
    r"caption|credit|descr|signature|підпис|подпис|author|source|copyright|byline|photographer"
    r"|photo[-_ ]?(?:source|author|desc|text|info)|img[-_ ]?(?:source|author|desc)"
    r"|image[-_ ]?(?:source|author|desc)|source[-_ ]?photo",
    re.I,
)
CREDIT_RE = re.compile(
    r"(?:Ілюстративне фото|Фото з відкритих джерел|Фото|Фотограф|Photo|Credit)"
    r"\s*[:：]\s*[^\n]{2,150}",
    re.I,
)


def _clean(t):
    return re.sub(r"\s+", " ", t or "").strip()


def _caption_from_text(t):
    """Підпис = текст елемента, якщо він короткий; інакше фрагмент 'Фото: ...'."""
    t = _clean(t)
    if not t:
        return None
    if len(t) <= _lim(200, 600):
        return t
    m = CREDIT_RE.search(t)
    return _clean(m.group(0)) if m else None


# ---------- Заборона підписів-джерел типу «Фото: pravda.com.ua» ----------
SITE_NAMES = {
    "українська правда", "українську правду", "укрправда",
    "24 канал", "канал 24", "телеканал 24", "24tv", "pravda",
    "eurointegration", "європейська правда", "європейської правди",
    "економічна правда", "економічної правди", "нв", "nv", "sport.nv.ua",
}
for _f in FEEDS:  # назви й домени джерел з FEEDS теж заборонені
    _host = urlparse(_f).netloc.lower().removeprefix("www.")
    if _host:
        SITE_NAMES.add(_host)
        SITE_NAMES.add(_host.split(".")[0])

# Заборонені домени: сайти з чорного списку + домени стрічок FEEDS.
# Решта доменів у підписах (наприклад facebook.com/Alexanderusyk) лишається.
_BLOCKED_HOSTS = set(BLOCK_LINK_HOSTS)
for _f in FEEDS:
    _h = urlparse(_f).netloc.lower().removeprefix("www.")
    if _h:
        _BLOCKED_HOSTS.add(_h)
BLOCKED_DOMAIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:[\w-]+\.)*(?:"
    + "|".join(re.escape(h) for h in sorted(_BLOCKED_HOSTS, key=len, reverse=True))
    + r")(?:/[^\s)\]]*)?",
    re.I,
)

DOMAIN_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:ua|com|net|org|info|ru|eu|tv|io|media|news)\b",
    re.I,
)


SITE_NAME_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(SITE_NAMES, key=len, reverse=True)) + r")(?!\w)",
    re.I,
)


def has_site_name(text):
    return bool(SITE_NAME_RE.search(text))


def is_site_part(text):
    """True, якщо фрагмент підпису містить заборонений сайт або назву сайту-джерела."""
    return bool(BLOCKED_DOMAIN_RE.search(text)) or has_site_name(text)


def sanitize_caption(caption):
    """Прибирає з підпису лише сайти-джерела (pravda.com.ua, nv.ua тощо).
    'Олександр Усик із дружиною (Фото: facebook.com/Alexanderusyk)' -> без змін;
    'Фото: pravda.com.ua' -> None;
    'Фото: Запорізька ОВА / pravda.com.ua' -> 'Фото: Запорізька ОВА'."""
    caption = _clean(caption)
    if not caption:
        return None
    if not is_site_part(caption):
        return caption  # немає заборонених сайтів: підпис лишається повним
    prefix, sep, rest = caption.partition(":")
    if not sep:
        names = "|".join(re.escape(n) for n in SITE_NAMES)
        cleaned = re.sub(
            r"[,.;]?\s*(?:ілюстративне\s+)?(?:фото|скріншот)\s*:?\s*[«\"]?(?:"
            + BLOCKED_DOMAIN_RE.pattern + "|" + names + r")[»\"]?",
            "", caption, flags=re.I,
        )
        cleaned = _clean(cleaned).strip(" ,;.")
        return None if (not cleaned or is_site_part(cleaned)) else cleaned
    rest2 = BLOCKED_DOMAIN_RE.sub(" / ", rest)
    parts = [x.strip() for x in re.split(r"\s*(?:\s/\s|[|;,])\s*", rest2) if x.strip()]
    good = [x for x in parts if not has_site_name(x)]
    if not good:
        return None
    return f"{prefix.strip()}: {', '.join(good)}"


CREDIT_START_RE = re.compile(
    r"^\s*(?:Ілюстративне фото|Фото з відкритих джерел|Фото|Фотограф|Photo|Credit)\s*[:：]",
    re.I,
)
# підпис, що закінчується вказівкою автора: «..., фото ОВА», «скріншот: X», «ілюстративне фото Getty Images»
CAPTION_END_RE = re.compile(
    r"(?:^|[,.;:—–(]\s+|[,.;:—–(])(?:ілюстративне\s+|архівне\s+)?"
    r"(?:фото|скріншот|кадри?|відео|ілюстраці[яї]|світлина)(?=[\s:])\s*:?\s*"
    r"(?-i:[A-ZА-ЯІЇЄҐ])[^.,]{0,45}\.?\s*\)?\s*$",
    re.I,
)
INLINE_TAGS = {"span", "a", "b", "i", "em", "strong", "small", "br", "u", "font", "abbr", "time"}
IMG_ATTRS = ("src", "data-src", "data-lazy-src", "srcset", "data-srcset")


def _text(el):
    t = _clean(" ".join(el.itertext()))
    t = re.sub(r"\(\s+", "(", t)  # без пробілу після «(»
    return re.sub(r"\s+([)\].,;:!?])", r"\1", t)  # і перед «)», розділовими знаками


def looks_like_caption(t):
    """Короткий текст, схожий на підпис під фото (є вказівка «фото ...», «скріншот: ...»)."""
    t = _clean(t)
    if not t or len(t) > _lim(250, 600):
        return False
    return bool(CREDIT_START_RE.match(t) or CAPTION_END_RE.search(t))


def _is_credit_el(el):
    """Елемент, весь текст якого схожий на підпис (без вкладених абзаців)."""
    if not isinstance(el.tag, str) or el.tag in ("script", "style"):
        return None
    if any(not isinstance(ch.tag, str) or ch.tag not in INLINE_TAGS for ch in el):
        return None
    t = _text(el)
    if not looks_like_caption(t):
        return None
    if el.tag == "p" and not CREDIT_START_RE.match(t):
        return None  # звичайні абзаци не вважаємо підписами
    return t


def _caption_parts(parent, img):
    """Тексти всіх елементів-підписів у контейнері (без вкладених дублів)."""
    hits = []
    for el in parent.iter():
        if el is img or not isinstance(el.tag, str) or el.tag in ("script", "style"):
            continue
        cls = (el.get("class") or "") + " " + (el.get("id") or "")
        if el.tag == "figcaption" or CAPTION_HINT.search(cls):
            hits.append(el)
    hs = set(hits)
    texts = []
    for el in hits:
        if any(a in hs for a in el.iterancestors()):
            continue
        t = _text(el)
        if t and t not in texts:
            texts.append(t)
    return texts


def _join_caption(texts):
    """Опис + джерело: 'Опис (Фото: джерело)'."""
    if not texts:
        return None
    main = texts[0]
    for t in texts[1:]:
        if t.lower() in main.lower():
            continue
        if CREDIT_START_RE.match(t):
            t = f"({t})"
        elif len(t) <= 60:
            t = f"(Фото: {t})"
        main = f"{main} {t}"
    return main if len(main) <= _lim(300, 700) else _caption_from_text(main)


def _structural_caption(img):
    """Підпис, що за будовою сторінки належить саме цьому фото:
    figcaption, блок з класом caption/descr..., або найменший контейнер
    навколо фото з коротким текстом."""
    node = img
    for _ in range(5):
        parent = node.getparent()
        if parent is None or not isinstance(parent.tag, str):
            break
        if parent.tag in ("body", "html", "article", "main"):
            break
        if len(parent.xpath(".//img")) > 1:
            break  # у блоці кілька фото: підпис неоднозначний
        c = _join_caption(_caption_parts(parent, img))
        if c:
            return c
        t = _text(parent)
        if (
            t
            and len(t) <= _lim(300, 600)
            and not parent.xpath(".//p|.//h1|.//h2|.//h3|.//ul|.//ol")
            and not re.fullmatch(r"[\d\s.:/,\-]+", t)  # лише дата/час: не підпис
        ):
            return t
        node = parent
    return None


def _sibling_caption(img):
    """Короткий рядок «..., фото ОВА» одразу після фото (або після його обгортки)."""
    node = img
    for _ in range(3):
        sib = node.getnext()
        while sib is not None and not isinstance(sib.tag, str):
            sib = sib.getnext()
        if (
            sib is not None
            and sib.tag not in ("script", "style", "img", "picture", "figure")
            and not sib.xpath(".//img")
        ):
            t = _text(sib)
            if looks_like_caption(t):
                return t
        node = node.getparent()
        if node is None or not isinstance(node.tag, str) or node.tag in ("body", "html", "article", "main"):
            break
    return None


def _matching_imgs(doc, image_url):
    stem = _stem(image_url or "")  # ім'я файлу без розширення й суфіксів розміру
    if len(stem) < 5:
        return []
    res = []
    for img in doc.iter("img"):
        val = " ".join(img.get(a) or "" for a in IMG_ATTRS + ("data-original",)).lower()
        if stem in val:
            res.append(img)
    return res


def debug_image_context(html_text, image_url):
    """Фрагмент HTML навколо фото (для діагностики, коли підпис не знайдено)."""
    try:
        doc = lxml_html.fromstring(html_text)
        imgs = _matching_imgs(doc, image_url)
        if not imgs:
            return "(зображення на сторінці не знайдено за назвою файлу)"
        node = imgs[0].getparent()
        if node is not None and node.getparent() is not None and len(_text(node)) < 5:
            node = node.getparent()
        return lxml_html.tostring(node, encoding="unicode")[:900]
    except Exception as e:
        return f"(не вдалося: {e})"


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _jsonld_caption(doc, image_url, is_main):
    """Підпис до фото з розмітки JSON-LD (ImageObject), якщо його немає в HTML."""
    stem = _stem(image_url or "")
    fallback = None
    for s in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(s)
        except Exception:
            continue
        for d in _walk(data):
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if "ImageObject" not in types:
                continue
            url = d.get("url") or d.get("contentUrl") or ""
            matched = bool(stem) and isinstance(url, str) and stem in url.lower()
            cap = d.get("caption") or (d.get("description") if matched else "")
            cap = _clean(cap) if isinstance(cap, str) else ""
            credit = d.get("creditText")
            if not isinstance(credit, str):
                credit = ""
            credit = _clean(credit)
            text = cap
            if credit and credit.lower() not in cap.lower():
                text = (cap + ". " if cap else "") + "Фото: " + credit
            if not text:
                continue
            if matched:
                return text
            if is_main and fallback is None:
                fallback = text
    return fallback


def find_photo_caption(html_text, image_url, is_main=False):
    """Шукає на сторінці новини підпис до фото так, як він написаний на сайті
    (наприклад: «наслідки атаки, фото ОВА» чи «Фото: Getty Images»)."""
    if not html_text:
        return None
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return None

    imgs = _matching_imgs(doc, image_url)
    target = imgs[0] if imgs else None

    # 1) Підпис, що за будовою сторінки належить цьому фото
    for img in imgs:
        c = _structural_caption(img)
        if c:
            return c
    # 2) Рядок із вказівкою «фото ...» одразу після фото
    if target is not None:
        c = _sibling_caption(target)
        if c:
            return c

    # 3) Найближчий до фото елемент-підпис
    elements = [
        el for el in doc.iter() if isinstance(el.tag, str) and el.tag not in ("script", "style")
    ]
    pos = {el: i for i, el in enumerate(elements)}
    credits = []
    for el in elements:
        t = _is_credit_el(el)
        if t:
            credits.append((pos[el], t, el))

    if target is not None and target in pos:
        ti = pos[target]
        img_pos = [pos[e] for e in elements if e.tag == "img"]
        best = None
        for ci, t, _el in credits:
            # підпис належить найближчому до нього фото; чужі підписи не беремо
            if min(img_pos, key=lambda x: abs(x - ci)) != ti:
                continue
            d = ci - ti
            if 0 <= d <= 12 or -4 <= d < 0:
                score = d if d >= 0 else 100 - d  # підписи після фото в пріоритеті
                if best is None or score < best[0]:
                    best = (score, t)
        if best:
            return best[1]

    # 4) Для головного фото: перший підпис у статті
    if is_main and credits:
        scope = doc.xpath("//article")
        if scope:
            inside = set(scope[0].iter())
            for _ci, t, el in credits:
                if el in inside:
                    return t
        else:
            return credits[0][1]

    # 5) JSON-LD (ImageObject)
    c = _jsonld_caption(doc, image_url, is_main)
    if c:
        return c

    # 6) Головне фото: перший непорожній figcaption у статті
    if is_main:
        scope = doc.xpath("//article") or [doc]
        for fc in scope[0].iter("figcaption"):
            c = _caption_from_text(_text(fc))
            if c:
                return c
    return None


# ---------- Заборона фото Getty Images у тілі статті ----------
# У тексті: Getty Images / GETTY IMAGES / Getty / GettyImages / Гетті (але не «Gettysburg»)
GETTY_RE = re.compile(r"(?<!\w)(?:getty(?:[\s_-]*images)?|гетт[иі]\w*)(?!\w)", re.I)
# В адресі картинки: gettyimages.com, .../getty_123.jpg
GETTY_URL_RE = re.compile(r"gettyimages|(?<![a-z])getty(?![a-z])", re.I)


def is_getty_image(html_text, image_url):
    """True, якщо фото на сторінці-першоджерелі підписане Getty Images
    (у підписі, alt/title, адресі картинки або в тексті поруч із фото)."""
    if GETTY_URL_RE.search(image_url or ""):
        return True
    if not html_text:
        return False
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return False

    # 1) підпис, знайдений звичайним способом (до очищення sanitize_caption)
    if GETTY_RE.search(find_photo_caption(html_text, image_url) or ""):
        return True

    for img in _matching_imgs(doc, image_url):
        # 2) alt / title самого зображення
        if GETTY_RE.search((img.get("alt") or "") + " " + (img.get("title") or "")):
            return True
        # 3) короткий текст у найближчих контейнерах навколо фото
        node = img
        for _ in range(3):
            node = node.getparent()
            if node is None or not isinstance(node.tag, str) or node.tag in ("body", "html", "article", "main"):
                break
            if len(node.xpath(".//img")) > 1:
                break
            t = _text(node)
            if len(t) <= 400 and GETTY_RE.search(t):
                return True
        # 4) рядок «Фото: Getty Images» одразу після фото
        if GETTY_RE.search(_sibling_caption(img) or ""):
            return True
    return False


def main_photo_is_getty(page_html, img_url, *captions):
    """Головне фото від Getty Images: перевіряємо сторінку, адресу та всі підписи
    (зі сторінки й з RSS)."""
    if is_getty_image(page_html, img_url):
        return True
    return any(GETTY_RE.search(c or "") for c in captions)


def rss_caption(entry):
    """Підпис до фото, який є безпосередньо в RSS-записі новини
    (media:credit, підписи в описі, alt зображення тощо)."""
    cands = []

    mc = entry.get("media_credit")
    if mc:
        for it in (mc if isinstance(mc, list) else [mc]):
            val = it.get("content") if isinstance(it, dict) else str(it)
            if val and _clean(val):
                cands.append("Фото: " + _clean(val))  # media:credit містить лише автора/джерело

    for key in ("media_content", "enclosures"):
        for it in entry.get(key, []) or []:
            if isinstance(it, dict):
                for k in ("credit", "description"):
                    v = it.get(k)
                    if isinstance(v, str):
                        c = _caption_from_text(v)
                        if c:
                            cands.append(c)

    htmls = []
    for c in entry.get("content", []) or []:
        if isinstance(c, dict) and c.get("value"):
            htmls.append(c["value"])
    for k in ("summary", "description"):
        if entry.get(k):
            htmls.append(entry[k])

    for raw in htmls:
        try:
            doc = lxml_html.fromstring("<div>" + raw + "</div>")
        except Exception:
            continue
        for fc in doc.iter("figcaption"):
            c = _caption_from_text(_text(fc))
            if c:
                cands.append(c)
        for img in doc.iter("img"):
            for a in ("alt", "title"):
                v = _clean(img.get(a) or "")
                if v and len(v) <= 200 and CREDIT_START_RE.match(v):
                    cands.append(v)
        for el in doc.iter():
            t = _is_credit_el(el)
            if t:
                cands.append(t)

    for c in cands:
        c = sanitize_caption(c)
        if c:
            return c
    return None


# ---------- Додаткові фото зі статті ----------
SKIP_SRC = re.compile(
    r"logo|icon|avatar|sprite|pixel|spacer|emoji|placeholder|blank|1x1|favicon|banner|/ads?/|\.svg",
    re.I,
)
SKIP_CONTAINER = re.compile(
    r"related|recommend|sidebar|comment|share|banner|widget|promo|advert"
    r"|(?<![a-z])ads?(?![a-z])|newsletter|subscribe|popular|read-?also|also-?read|author|footer|menu",
    re.I,
)


def _best_src(img):
    """Адреса найбільшої версії картинки (враховує srcset і lazy-load)."""
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        best, best_w = None, -1
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = 0
            if len(bits) > 1:
                m = re.match(r"(\d+)", bits[1])
                w = int(m.group(1)) if m else 0
            if w > best_w:
                best, best_w = bits[0], w
        if best:
            return best
    for a in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(a)
        if v and not v.startswith("data:"):
            return v
    return None


def _stem(url):
    """Ім'я файлу без розширення й суфіксів розміру (щоб знаходити ту саму картинку)."""
    name = url.split("?")[0].rstrip("/").split("/")[-1].lower()
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name)
    name = re.sub(r"[-_]\d+x\d+$", "", name)
    name = re.sub(r"[-_](?:small|medium|large|big|thumb|orig|original|preview)$", "", name)
    return name


def find_article_images(html_text, page_url, exclude):
    """Знаходить у тексті новини додаткові фото (без головного, логотипів,
    реклами й блоків «читайте також»). Повертає список адрес."""
    if not html_text:
        return []
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return []

    scope = doc.xpath("//article")
    root = scope[0] if scope else doc
    skip_stems = {_stem(u) for u in exclude if u}
    result = []

    for img in root.iter("img"):
        # занадто малі картинки (іконки, смайли)
        if any(
            (img.get(a) or "").isdigit() and int(img.get(a)) < 200
            for a in ("width", "height")
        ):
            continue
        src = _best_src(img)
        if not src:
            continue
        url = urljoin(page_url, src)
        if not url.startswith("http") or SKIP_SRC.search(url):
            continue

        # пропускаємо картинки з бокових блоків, реклами, схожих новин
        bad = False
        for anc in img.iterancestors():
            if anc is root or not isinstance(anc.tag, str) or anc.tag in ("body", "html"):
                break
            if anc.tag in ("aside", "footer", "nav"):
                bad = True
                break
            if SKIP_CONTAINER.search((anc.get("class") or "") + " " + (anc.get("id") or "")):
                bad = True
                break
        if bad:
            continue

        st = _stem(url)
        if not st or st in skip_stems:
            continue
        skip_stems.add(st)
        result.append(url)
        if len(result) >= MAX_IMAGES:
            break
    return result


def entry_image(entry):
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key)
        if items and items[0].get("url"):
            return items[0]["url"]
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    return None


# ---------- Перевірка цитат ----------
_n = MIN_QUOTE_LEN
QUOTE_RE = re.compile(
    r"«([^»]{%d,})»|„([^“”]{%d,})[“”]|“([^”]{%d,})”|\"([^\"]{%d,})\"" % (_n, _n, _n, _n)
)

# рядки-«сміття», які не є частиною новини (посилання на інші матеріали тощо)
NOISE_RE = re.compile(
    r"^\s*(?:читайте також|дивіться також|також читайте|читайте ще|читайте далі"
    r"|за темою|джерело|фото|відео)\b.*$",
    re.I | re.M,
)


def strip_noise(text):
    return NOISE_RE.sub("", text)


def norm(s):
    """Лишає тільки літери й цифри в нижньому регістрі (ігнорує пунктуацію)."""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def missing_quotes(original, rewritten):
    """Повертає цитати з оригіналу, яких немає дослівно в новому тексті."""
    new_norm = norm(rewritten)
    missing = []
    for m in QUOTE_RE.finditer(original):
        quote = next(g for g in m.groups() if g)
        if norm(quote) not in new_norm:
            missing.append(quote.strip())
    return missing


def rewrite(title, text, extra=""):
    user_msg = f"Заголовок: {title}\n\nТекст:\n{text[:8000]}"
    if extra:
        user_msg += "\n\n" + extra
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.3,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    return json.loads(resp.choices[0].message.content)


# ---------- Посилання на джерела в тексті ----------
SHARE_RE = re.compile(
    r"sharer|/share[/?]|/intent/|share\.php|addthis|^mailto:|^tel:|^javascript:|^#", re.I
)
CTA_RE = re.compile(
    r"підписуйт|підписатис|підпишіть|читайте нас|наш телеграм|наш канал|приєднуйтес|слідкуйте",
    re.I,
)


def _host(u):
    return urlparse(u).netloc.lower().removeprefix("www.")


def url_key(u):
    """Нормалізована адреса для порівняння (без http/www, # і кінцевого /)."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("#")[0].rstrip("/")


# ---------- Соцмережі: хто саме повідомляє і де ----------
SOCIAL_NETS = {
    "t.me": ("Telegram", "у"), "telegram.me": ("Telegram", "у"),
    "facebook.com": ("Facebook", "у"), "fb.com": ("Facebook", "у"), "fb.watch": ("Facebook", "у"),
    "x.com": ("X", "в"), "twitter.com": ("X", "в"),
    "instagram.com": ("Instagram", "в"),
    "youtube.com": ("YouTube", "на"), "youtu.be": ("YouTube", "на"),
    "tiktok.com": ("TikTok", "у"), "threads.net": ("Threads", "у"), "threads.com": ("Threads", "у"),
    "linkedin.com": ("LinkedIn", "у"), "truthsocial.com": ("Truth Social", "у"),
    "bsky.app": ("Bluesky", "у"),
}
NET_ALIASES = {
    "Telegram": r"telegram|телеграм\w*",
    "Facebook": r"facebook|фейсбук\w*",
    "X": r"(?<!\w)x(?!\w)|twitter|твіттер\w*",
    "Instagram": r"instagram|інстаграм\w*",
    "YouTube": r"youtube|ютуб\w*",
    "TikTok": r"tiktok|тікток\w*",
    "Threads": r"threads",
    "LinkedIn": r"linkedin",
    "Truth Social": r"truth\s*social",
    "Bluesky": r"bluesky",
}
NET_WORD_RE = re.compile(
    r"(?<!\w)(?:telegram|телеграм\w*|facebook|фейсбук\w*|твіттер\w*|twitter|instagram|інстаграм\w*"
    r"|threads|tiktok|youtube|ютуб\w*|x)(?!\w)", re.I)
GENERIC_WORD_RE = re.compile(
    r"(?:повідом\w*|написа\w*|заяв\w*|інформ\w*|зазнач\w*|розпові\w*|уточн\w*|пише\w*|каже|тут"
    r"|джерело|посилання|публікаці\w*|допис\w*|пост\w*|повідомленн\w*|відео|фото|це"
    r"|у|в|на|за|та|і|й|свої\w*|акаунт\w*|канал\w*|сторінк\w*|сторінц\w*|соцмереж\w*|мереж\w*)",
    re.I)
SUBJ_RE = re.compile(
    r"(?:повідом\w+|написа\w+|заяв\w+|інформ\w+|зазнач\w+|розпові\w+|уточн\w+|додал\w+|пише\w*)\s+"
    r"(.{3,90}?)"
    r"(?=\s+(?:у|в|на|через)\s+(?:своє\w*\s+)?(?:telegram|телеграм|facebook|фейсбук|x|twitter|твіттер"
    r"|instagram|інстаграм|threads|tiktok|youtube|акаунт|сторінц|канал|соцмереж)|[,.;:]|$)",
    re.I)


def social_network(url):
    h = _host(url)
    for k, v in SOCIAL_NETS.items():
        if h == k or h.endswith("." + k):
            return v
    return None


def social_handle(url):
    segs = [s for s in urlparse(url).path.split("/") if s]
    if segs and segs[0] == "s":
        segs = segs[1:]
    if not segs or segs[0].lower() in {
        "share", "intent", "profile.php", "watch", "i", "web", "hashtag", "channel", "c", "user", "status"
    }:
        return ""
    if _host(url).endswith(("youtube.com", "youtu.be")):
        return ""
    return segs[0].lstrip("@")


def is_generic_anchor(anchor):
    """«повідомив», «написав та повідомив», «у Telegram», «тут» тощо, тобто текст без назви джерела."""
    a = NET_WORD_RE.sub(" ", (anchor or "").lower().strip(" «»„“”\".,:;"))
    words = [w for w in re.split(r"[\s,]+", a) if w]
    return all(GENERIC_WORD_RE.fullmatch(w) for w in words)


def source_label(anchor, url, context):
    """Назва джерела для посилання. Якщо якір це дієслово («повідомив»), шукаємо, хто саме."""
    if not is_generic_anchor(anchor):
        return anchor
    net = social_network(url)
    if not net:
        return brand_from_url(url)
    m = SUBJ_RE.search(context or "")
    if m:
        who = NET_WORD_RE.sub("", m.group(1)).strip(" ,.;:«»„“”\"")
        if 3 <= len(who) <= 90 and not is_generic_anchor(who):
            return who
    return social_handle(url) or net[0]


def find_source_links(html_text, page_url):
    """Зовнішні посилання на джерела в тексті статті (наприклад, «Про це пише ERR»).
    Повертає список {anchor, url, context, network, prep, handle}. Посилання на сам
    сайт-джерело, меню, кнопки «поділитися» та заклики підписатися пропускаються."""
    if not html_text:
        return []
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return []

    found = doc.xpath("//article")
    scope = found[0] if found else doc
    page_host = _host(page_url)
    result, seen_keys = [], set()

    for a in scope.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or SHARE_RE.search(href):
            continue
        url = urljoin(page_url, href)
        if not url.lower().startswith("http"):
            continue
        host = _host(url)
        if not host or host == page_host or host.endswith("." + page_host):
            continue
        if any(host == h or host.endswith("." + h) for h in BLOCK_LINK_HOSTS):
            continue  # посилання на сайти з чорного списку (pravda.com.ua тощо) не зберігаємо
        anchor = _text(a)
        if not anchor or len(anchor) > 150:
            continue

        par, bad = None, False
        for anc in a.iterancestors():
            if anc is scope or not isinstance(anc.tag, str) or anc.tag in ("body", "html"):
                break
            if anc.tag in ("aside", "footer", "nav"):
                bad = True
                break
            if SKIP_CONTAINER.search((anc.get("class") or "") + " " + (anc.get("id") or "")):
                bad = True
                break
            if par is None and anc.tag in ("p", "blockquote", "li"):
                par = anc
        if bad or par is None:
            continue
        context = _text(par)
        if CTA_RE.search(context):
            continue

        key = url_key(url)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        net = social_network(url)
        result.append({
            "anchor": source_label(anchor, url, context),
            "url": url,
            "context": context[:250],
            "network": net[0] if net else "",
            "prep": net[1] if net else "",
            "handle": social_handle(url) if net else "",
        })
        if len(result) >= 8:
            break
    return result


def format_links(links):
    if not links:
        return ""
    lines = [
        "Посилання з оригіналу. Кожне з них ОБОВ'ЯЗКОВО збережи в переписаному тексті "
        'як HTML-посилання <a href="адреса" target="_blank" rel="noopener">слово або '
        "назва, на яких воно стоїть</a> (адресу не змінюй). Згадки про те, хто повідомив "
        "чи заявив, також залишай. Якщо в оригіналі посилання подано рядком "
        "«Посилання: ...» чи «Джерело: ...», перепиши його розгорнутим реченням "
        "(«За інформацією ...», «Про це повідомляє ...»), а не копіюй рядок:"
    ]
    for i, l in enumerate(links, 1):
        net = f" [соцмережа: {l['network']}; акаунт: {l['handle'] or 'невідомо'}]" if l.get("network") else ""
        lines.append(f"{i}. «{l['anchor']}» → {l['url']}{net} (абзац в оригіналі: «{l['context']}»)")
    return "\n".join(lines)


A_TAG_RE = re.compile(r"<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def clean_links(content, links):
    """Лишає тільки посилання з оригіналу (вигадані прибирає). Посилання на дієсловах
    («повідомив», «написав», «тут») теж знімає: їх додасть коректне речення про джерело."""
    allowed = {url_key(l["url"]) for l in links}

    def repl(m):
        href, inner = m.group(1), m.group(2)
        if href.lower().startswith("http") and url_key(href) in allowed:
            if is_generic_anchor(re.sub(r"<[^>]+>", "", inner)):
                return inner
            return m.group(0)
        return inner

    return A_TAG_RE.sub(repl, content)


def missing_links(links, content):
    present = {url_key(m.group(1)) for m in A_TAG_RE.finditer(content)}
    return [l for l in links if url_key(l["url"]) not in present]


def _a(url, text):
    return f'<a href="{escape(url)}" target="_blank" rel="noopener">{escape(text)}</a>'


def _and(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " та " + items[-1]


def add_missing_links(content, missing):
    """Втрачені посилання додаємо розгорнутим реченням другим абзацом:
    «Про це повідомляє <a>КМВА</a> у Telegram.» (соцмережі), «За інформацією <a>ERR</a>.» (решта)."""
    soc, other, channels = [], [], {}
    for l in missing:
        if l.get("network"):
            ch = (l["network"], (l.get("handle") or l["anchor"]).lower())
            if ch in channels:
                channels[ch]["extra"].append(l["url"])  # той самий акаунт: ще один допис
                continue
            item = {"html": _a(l["url"], l["anchor"]), "net": l["network"],
                    "prep": l.get("prep", "у"), "anchor": l["anchor"], "extra": []}
            channels[ch] = item
            soc.append(item)
        else:
            other.append(_a(l["url"], l["anchor"]))

    def soc_text(it):
        s = it["html"]
        if it["anchor"].lower() != it["net"].lower():
            s += f" {it['prep']} {escape(it['net'])}"
        if it["extra"]:
            s += " (" + ", ".join(_a(u, f"допис {i}") for i, u in enumerate(it["extra"], 2)) + ")"
        return s

    parts = []
    if soc:
        parts.append("Про це повідомляє " + _and([soc_text(i) for i in soc]))
    if other:
        parts.append("За інформацією " + _and(other))
    sentence = "<p>" + ". ".join(parts) + ".</p>"
    i = content.lower().find("</p>")
    if i == -1:
        return sentence + "\n" + content
    i += len("</p>")
    return content[:i] + "\n" + sentence + content[i:]


def ensure_network(content, links):
    """Якщо посилання веде в соцмережу, а в абзаці її не названо, додає «у Telegram» тощо."""
    meta = {url_key(l["url"]): l for l in links if l.get("network")}
    if not meta:
        return content

    def fix_par(pm):
        par = pm.group(0)
        plain = re.sub(r"<[^>]+>", " ", par)
        done = set()

        def repl(m):
            l = meta.get(url_key(m.group(1)))
            if not l or l["network"] in done:
                return m.group(0)
            alias = NET_ALIASES.get(l["network"], re.escape(l["network"]))
            if re.search(alias, plain, re.I):
                done.add(l["network"])
                return m.group(0)
            done.add(l["network"])
            return m.group(0) + f" {l['prep']} {l['network']}"

        return A_TAG_RE.sub(repl, par)

    return UNIT_RE.sub(fix_par, content)


# ---------- Абзац із джерелом завжди другий ----------
SOURCE_PAR_RE = re.compile(
    r"^(?:(?:Також\s+)?про\s+це(?:\s+також)?\s+(?!не\b)(?:[\w'’ʼ-]+\s+){0,2}?"
    r"(?:повідом|пише|писал|написа|свідч|йдеться|інформ|розпові|зазнач|заяв|уточн|додал)\w*"
    r"|за\s+інформацією|за\s+повідомленням|це\s+повідомляє"
    r"|повідомляє|повідомляють|інформує|пише|джерело)(?![\w-])",
    re.I,
)


def _plain_par(p):
    return _clean(unescape(re.sub(r"<[^>]+>", " ", p)))


def is_source_par(p):
    """Короткий абзац-посилання на джерело: «Про це повідомляє Reuters.», «Про це свідчить
    дослідження компанії Gradus.», «Про це повідомляє Sun Boxing на YouTube.» тощо."""
    plain = _plain_par(p)
    return bool(plain) and len(plain) <= 300 and bool(SOURCE_PAR_RE.match(plain))


def move_source_to_second(content):
    """Абзаци із джерелом ставимо одразу після першого абзацу (другими),
    а не в кінець статті. Решта порядку не змінюється."""
    parts = UNIT_RE.split(content)
    if any(parts[i].strip() for i in range(0, len(parts), 2)):
        return content  # є текст поза <p>/<blockquote>: не чіпаємо
    units = [parts[i] for i in range(1, len(parts), 2)]
    first = next((i for i, u in enumerate(units) if u.lower().startswith("<p")), None)
    if first is None:
        return content
    src = [
        i for i, u in enumerate(units)
        if i > first and u.lower().startswith("<p") and is_source_par(u)
    ]
    if not src or src == list(range(first + 1, first + 1 + len(src))):
        return content  # джерела немає або воно вже другим
    moved = [units[i] for i in src]
    rest = [u for i, u in enumerate(units) if i not in set(src)]
    new = rest[: first + 1] + moved + rest[first + 1:]
    print("Абзац із джерелом переставлено на друге місце")
    return "\n".join(new)


def add_target_blank(content):
    """Посилання відкриваються в новій вкладці."""
    return re.sub(r"<a\s+(?![^>]*\btarget=)", '<a target="_blank" rel="noopener" ', content, flags=re.I)


# ---------- Дослівні цитати: підставляємо оригінал у готовий текст ----------
QUOTE_SUB_RE = re.compile(
    r"«([^»\n]{%d,600})»|„([^“”\n]{%d,600})[“”]|“([^”\n]{%d,600})”|\"([^\"\n]{%d,600})\""
    % (MIN_QUOTE_LEN, MIN_QUOTE_LEN, MIN_QUOTE_LEN, MIN_QUOTE_LEN)
)
MARK_RE = re.compile(r"\[\[\s*Q(\d+)\s*\]\]", re.I)
WRAPPED_MARK_RE = re.compile(r"[«„“\"]\s*(\[\[\s*Q\d+\s*\]\])\s*[»“”\"]", re.I)


def mask_quotes(text):
    """Замінює прямі цитати маркерами [[Q1]], [[Q2]]...
    Повертає (текст з маркерами, список оригінальних цитат разом з лапками)."""
    quotes = []

    def repl(m):
        quotes.append(m.group(0))
        return f"[[Q{len(quotes)}]]"

    return QUOTE_SUB_RE.sub(repl, text), quotes


def unmask_quotes(content, quotes):
    """Підставляє замість маркерів точні оригінальні цитати.
    Повертає (текст, множина використаних номерів)."""
    content = WRAPPED_MARK_RE.sub(r"\1", content)  # GPT міг додати лапки навколо маркера
    used = set()

    def repl(m):
        i = int(m.group(1))
        if 1 <= i <= len(quotes):
            used.add(i)
            return escape(quotes[i - 1], quote=False)  # дослівно, як в оригіналі
        return ""

    return MARK_RE.sub(repl, content), used


def sentence_with(text, quote):
    """Речення з оригіналу, в якому стоїть цитата (разом з тим, хто її сказав)."""
    i = text.find(quote)
    if i < 0:
        return quote
    start = max(text.rfind(". ", 0, i), text.rfind("\n", 0, i)) + 1
    ends = [e for e in (text.find(". ", i + len(quote)), text.find("\n", i + len(quote))) if e != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()[:700]


# ---------- Видалення згадок видання-джерела («Європейська правда» тощо) ----------
OUTLET_NAMES = [
    "Українська правда. Життя", "Українська правда", "Української правди",
    "Європейська правда", "Європейської правди", "Економічна правда",
    "Економічної правди", "Європравда",
    "www.pravda.com.ua", "pravda.com.ua",
    "www.eurointegration.com.ua", "eurointegration.com.ua", "Eurointegration",
    "www.suspilne.media", "suspilne.media",
    "www.epravda.com.ua", "epravda.com.ua",
    "www.sport.nv.ua", "sport.nv.ua", "www.nv.ua", "nv.ua", "NV", "НВ",
    "NV Sport", "NV Спорт", "НВ Спорт", "Sport.NV",
]
BLOCKED_URL_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?(?:[\w-]+\.)*(?:"
    + "|".join(re.escape(h) for h in BLOCK_LINK_HOSTS)
    + r")(?:/[^\s<>\"'«»]*)?(?![\w-])",
    re.I,
)
_OUT = "(?:" + "|".join(
    r"(?<!\w)" + re.escape(n) + r"(?!\w)" for n in sorted(OUTLET_NAMES, key=len, reverse=True)
) + ")"
OUTLET_REF = (
    r"(?:(?:видання|ресурс|портал|сайт)[ \t]+)?[«„“\"]?[ \t]*" + _OUT + r"(?:[ \t]*[»”“\"])?"
)
OUTLET_ANY_RE = re.compile(_OUT, re.I)
_VERBS = (
    r"(?:інформує|повідомляє|пише|передає|розповідає|зазначає|наводить|цитує"
    r"|інформують|повідомляють|пишуть)"
)
_VIA = r"(?:з посиланням на|із посиланням на|посилаючись на)"

# «повідомляє Європейська правда з посиланням на Bloomberg» -> «повідомляє Bloomberg»
RULE_A = re.compile(r"(" + _VERBS + r")[ \t]+" + OUTLET_REF + r"[ \t]+" + _VIA + r"[ \t]+", re.I)
# окреме речення «Про це інформує Європейська правда.» -> прибрати
RULE_F = re.compile(
    r"(?:(?<=[.!?])[ \t]+|^)Про це[ \t]+" + _VERBS + r"[ \t]+" + OUTLET_REF + r"[ \t]*\.",
    re.I | re.M,
)
# «..., інформує Європейська правда» -> прибрати
RULE_B = re.compile(r"(?:[,;][ \t]*|[ \t]+)(?:як[ \t]+)?" + _VERBS + r"[ \t]+" + OUTLET_REF, re.I)
# «Європейська правда» у лапках без дієслова -> просто назва (її приб'є GPT)
RULE_E = re.compile(r"[«„“\"][ \t]*(" + _OUT + r")[ \t]*[»”“\"]", re.I)


def clean_outlet_text(text):
    t = BLOCKED_URL_RE.sub("", text)  # голі адреси сайтів з чорного списку
    t = RULE_A.sub(r"\1 ", t)
    t = RULE_F.sub("", t)
    t = RULE_B.sub("", t)
    return RULE_E.sub(r"\1", t)


def brand_from_url(url):
    """bloomberg.com -> Bloomberg"""
    parts = _host(url).split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net"):
        base = parts[-3]
    elif len(parts) >= 2:
        base = parts[-2]
    else:
        base = parts[0]
    return base.capitalize()


def fix_outlet_links(links):
    """Якщо посилання стоїть на назві видання-джерела («Європейська правда» -> bloomberg.com),
    переносимо його на назву першоджерела (Bloomberg)."""
    for l in links:
        l["context"] = clean_outlet_text(l["context"])
        anchor = l["anchor"].strip(" «»„“”\"")
        if re.fullmatch(_OUT, anchor, re.I):
            brand = brand_from_url(l["url"])
            m = re.search(re.escape(brand), l["context"], re.I)
            l["anchor"] = m.group(0) if m else brand
    return links


def outlet_mentions(content):
    plain = re.sub(r"<[^>]+>", " ", content)
    return sorted({m.group(0) for m in OUTLET_ANY_RE.finditer(plain)})


def drop_blocked_links(content):
    """Прибирає <a> на заблоковані сайти. Якщо текст посилання це домен чи назва видання,
    прибирає його повністю, інакше лишає тільки текст."""
    def repl(m):
        href = m.group(1)
        h = _host(href) if href.lower().startswith("http") else ""
        if h and any(h == b or h.endswith("." + b) for b in BLOCK_LINK_HOSTS):
            inner = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if DOMAIN_RE.fullmatch(inner) or re.fullmatch(_OUT, inner.strip(" «»„“”\""), re.I):
                return ""
            return m.group(2)
        return m.group(0)

    return A_TAG_RE.sub(repl, content)


def final_outlet_cleanup(content):
    """Страховка: прибирає згадки видання-джерела, що лишилися в готовому HTML."""
    content = drop_blocked_links(content)

    def repl(m):
        inner = re.sub(r"<[^>]+>", "", m.group(2)).strip(" «»„“”\"")
        if re.fullmatch(_OUT, inner, re.I):
            prefix = m.group(0)[: m.start(2) - m.start(0)]
            return prefix + escape(brand_from_url(m.group(1))) + "</a>"
        return m.group(0)

    c = A_TAG_RE.sub(repl, content)
    c = clean_outlet_text(c)
    # «<a>Bloomberg</a> з посиланням на Bloomberg» -> «<a>Bloomberg</a>»
    c = re.sub(r"(<a\s[^>]*>([^<]+)</a>)\s+(?:з посиланням на|із посиланням на|посилаючись на)\s+\2", r"\1", c)
    return re.sub(r"<p>\s*Про це\s*\.\s*</p>\s*", "", c)


def rewrite_checked(title, text, links=None):
    """Переписує новину. Прямі цитати та посилання на джерела гарантовано
    переносяться з оригіналу без змін. Повертає (дані, статус_публікації)."""
    links = links or []
    text = strip_noise(text)  # прибираємо «читайте також» тощо до переписування
    text = clean_outlet_text(text)  # прибираємо згадки видання-джерела до маскування цитат
    links = fix_outlet_links(links)
    masked, quotes = mask_quotes(text)  # цитати йдуть до GPT як маркери
    notes = format_links(links)
    if quotes:
        notes += (
            "\n\nПрямі цитати в тексті замінено маркерами "
            + ", ".join(f"[[Q{i}]]" for i in range(1, len(quotes) + 1))
            + ". Використай кожен маркер у новині точно як є."
        )

    def attempt(extra):
        d = rewrite(title, masked, extra)
        content, used = unmask_quotes(d["content"], quotes)
        d["content"] = clean_links(content, links)
        for k in ("title", "excerpt"):
            d[k] = MARK_RE.sub("", d.get(k) or "").strip()
        return d, used

    data, used = attempt(notes)
    miss_q = [i for i in range(1, len(quotes) + 1) if i not in used]
    miss_l = missing_links(links, data["content"])
    miss_o = outlet_mentions(data["content"])

    if miss_q or miss_l or miss_o:
        extra = notes
        if miss_o:
            print("У тексті згадано видання-джерело, пробую ще раз:", miss_o)
            extra += (
                "\n\nУ попередній спробі лишилася згадка видання-джерела: "
                + ", ".join(f"«{x}»" for x in miss_o)
                + ". Прибери її (правило 11), залишивши лише першоджерело."
            )
        if miss_q:
            print("Не використано цитати, пробую ще раз:", [f"Q{i}" for i in miss_q])
            extra += (
                "\n\nУ попередній спробі не було використано маркери: "
                + ", ".join(f"[[Q{i}]]" for i in miss_q)
                + ". Кожен маркер має бути в тексті обов'язково, поруч із тим, хто це сказав."
            )
        if miss_l:
            print("Втрачено посилання, пробую ще раз:", [l["url"] for l in miss_l])
            extra += (
                "\n\nУ попередній спробі було втрачено ці посилання. Збережи їх "
                "обов'язково як <a href=...> на назві того, хто повідомляє "
                "(не на дієслові), із вказівкою мережі:\n- "
                + "\n- ".join(f"«{l['anchor']}» → {l['url']}" for l in miss_l)
            )
        data, used = attempt(extra)
        miss_q = [i for i in range(1, len(quotes) + 1) if i not in used]
        miss_l = missing_links(links, data["content"])

    data["content"] = final_outlet_cleanup(data["content"])
    data["content"] = ensure_network(data["content"], links)
    for k in ("title", "excerpt"):
        data[k] = clean_outlet_text(data.get(k) or "")
    if outlet_mentions(data["content"]):
        print("Увага: у тексті лишилася згадка видання-джерела:", outlet_mentions(data["content"]))

    if miss_q:  # гарантія: цитата не втрачається, додаємо її речення з оригіналу дослівно
        added = []
        for i in miss_q:
            sent = sentence_with(text, quotes[i - 1])
            if sent not in added:
                added.append(sent)
        print("Цитати додано дослівно з оригіналу:", len(miss_q))
        data["content"] += "".join(f"\n<p>{escape(x, quote=False)}</p>" for x in added)
    if miss_l:  # гарантія: посилання не втрачаються
        print("Посилання додано другим абзацом:", [l["url"] for l in miss_l])
        data["content"] = add_missing_links(data["content"], miss_l)
    data["content"] = move_source_to_second(data["content"])
    data["content"] = add_target_blank(data["content"])
    return data, POST_STATUS


def upload_image(url):
    """Завантажує картинку у WordPress, повертає (ID, адреса на вашому сайті)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
        resp = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            auth=auth,
            headers={
                "Content-Disposition": f'attachment; filename="news.{ext}"',
                "Content-Type": ctype,
            },
            data=r.content,
            timeout=60,
        )
        resp.raise_for_status()
        j = resp.json()
        return j["id"], j.get("source_url")
    except Exception as e:
        print("Картинку не завантажено:", e)
        return None, None


def build_figure(image_url, caption, alt):
    # підпис лише той, що знайдено на сайті-джерелі; немає підпису, немає й рядка
    figcaption = f"<figcaption>{escape(caption)}</figcaption>" if caption else ""
    return (
        '<figure class="wp-block-image size-large">'
        f'<img src="{escape(image_url)}" alt="{escape(alt)}"/>'
        f"{figcaption}"
        "</figure>"
    )


UNIT_RE = re.compile(r"(<blockquote\b.*?</blockquote>|<p\b.*?</p>)", re.S | re.I)
def insert_figures(content, figures, has_main=True):
    """Перше (головне) фото завжди на самому початку статті, перед першим абзацом.
    Додаткові фото: після 3-го, 5-го, 7-го... абзацу (якщо абзаців менше, у кінець)."""
    if not figures:
        return content
    if has_main:
        first, rest = figures[0], figures[1:]
    else:  # головне фото лише на обкладинці: усі фото в тілі йдуть як додаткові
        first, rest = "", list(figures)
    parts = UNIT_RE.split(content)
    n_par = sum(1 for i in range(1, len(parts), 2) if parts[i].lower().startswith("<p"))
    if n_par == 0:
        return "\n".join(figures) + "\n" + content

    slots = [3, 5, 7, 9, 11]
    fig_at = {}
    for k, fig in enumerate(rest):
        n = slots[k] if k < len(slots) else n_par
        fig_at.setdefault(min(n, n_par), []).append(fig)

    out, count = ([first + "\n"] if first else []), 0
    for i, part in enumerate(parts):
        out.append(part)
        if i % 2 == 1 and part.lower().startswith("<p"):
            count += 1
            for fig in fig_at.get(count, []):
                out.append("\n" + fig + "\n")
    return "".join(out)


def clean_tags(raw):
    """Лишає до 3 унікальних позначок великими літерами."""
    tags = []
    for t in raw or []:
        t = re.sub(r"[#,]", " ", str(t))
        t = re.sub(r"\s+", " ", t).strip().upper()
        if t and t not in tags:
            tags.append(t)
    return tags[:3]


def get_tag_ids(names):
    """Знаходить або створює позначки у WordPress, повертає їхні ID."""
    ids = []
    for name in clean_tags(names):
        try:
            r = requests.get(
                f"{WP_URL}/wp-json/wp/v2/tags",
                auth=auth,
                params={"search": name, "per_page": 20},
                timeout=30,
            )
            r.raise_for_status()
            found = next(
                (t["id"] for t in r.json() if t["name"].strip().upper() == name), None
            )
            if found is None:
                r = requests.post(
                    f"{WP_URL}/wp-json/wp/v2/tags",
                    auth=auth,
                    json={"name": name},
                    timeout=30,
                )
                if r.status_code == 400 and r.json().get("code") == "term_exists":
                    found = r.json()["data"]["term_id"]
                else:
                    r.raise_for_status()
                    found = r.json()["id"]
            if found not in ids:
                ids.append(found)
        except Exception as e:
            print("Позначку не додано:", name, "->", e)
    return ids


_CATEGORY_CACHE = {}


def get_category_id(name):
    """Знаходить категорію WordPress за назвою (регістр не важливий) або створює її."""
    key = name.strip().upper()
    if key in _CATEGORY_CACHE:
        return _CATEGORY_CACHE[key]
    cid = None
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            auth=auth,
            params={"search": name, "per_page": 50},
            timeout=30,
        )
        r.raise_for_status()
        for c in r.json():
            if unescape(c["name"]).strip().upper() == key:
                cid = c["id"]
                break
        if cid is None:
            r = requests.post(
                f"{WP_URL}/wp-json/wp/v2/categories", auth=auth, json={"name": name}, timeout=30
            )
            if r.status_code == 400 and r.json().get("code") == "term_exists":
                cid = r.json()["data"]["term_id"]
            else:
                r.raise_for_status()
                cid = r.json()["id"]
    except Exception as e:
        print("Категорію не знайдено й не створено:", name, "->", e)
    if cid is not None:
        _CATEGORY_CACHE[key] = cid
    return cid


# ---------- Відео YouTube ----------
YT_ID = r"[A-Za-z0-9_-]{11}"
YT_URL_RES = [
    re.compile(r"youtube(?:-nocookie)?\.com/(?:embed|shorts|live|v)/(" + YT_ID + r")(?![\w-])", re.I),
    re.compile(r"youtube(?:-nocookie)?\.com/watch\?[^\s\"'<>]*?\bv=(" + YT_ID + r")(?![\w-])", re.I),
    re.compile(r"youtu\.be/(" + YT_ID + r")(?![\w-])", re.I),
]
YT_ID_ATTRS = ("data-video-id", "data-videoid", "videoid", "data-youtube-id", "data-yt-id")
YT_URL_ATTRS = ("src", "data-src", "data-lazy-src", "href", "data-url", "data-embed", "data-video-url")


def youtube_id(text):
    """Ідентифікатор відео з адреси YouTube (embed, watch, youtu.be, shorts) або None."""
    for rx in YT_URL_RES:
        m = rx.search(text or "")
        if m and m.group(1) != "videoseries":  # videoseries = плейлист, не відео
            return m.group(1)
    return None


def find_youtube_ids(html_text, page_url=""):
    """Відео YouTube у тексті статті (iframe, lazy-load, посилання, lite-youtube)."""
    if not html_text:
        return []
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return []
    found = doc.xpath("//article")
    scope = found[0] if found else doc
    ids = []
    for el in scope.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style"):
            continue
        cands = []
        for a in YT_URL_ATTRS:
            v = el.get(a)
            if v:
                cands.append(youtube_id(v))
        for a in YT_ID_ATTRS:
            v = (el.get(a) or "").strip()
            if re.fullmatch(YT_ID, v):
                cands.append(v)
        cands = [c for c in cands if c]
        if not cands:
            continue
        # пропускаємо відео з блоків «читайте також», реклами, бічних колонок
        bad = False
        for anc in el.iterancestors():
            if anc is scope or not isinstance(anc.tag, str) or anc.tag in ("body", "html"):
                break
            if anc.tag in ("aside", "footer", "nav"):
                bad = True
                break
            if SKIP_CONTAINER.search((anc.get("class") or "") + " " + (anc.get("id") or "")):
                bad = True
                break
        if bad:
            continue
        for c in cands:
            if c not in ids:
                ids.append(c)
    return ids[:MAX_VIDEOS] if MAX_VIDEOS > 0 else []


def youtube_ids_from_entry(entry):
    """Запасний варіант: відео YouTube з самого RSS-запису (iframe/посилання в описі)."""
    raws = []
    for c in entry.get("content", []) or []:
        if isinstance(c, dict) and c.get("value"):
            raws.append(c["value"])
    for k in ("summary", "description"):
        if entry.get(k):
            raws.append(entry[k])
    ids = []
    for raw in raws:
        for rx in YT_URL_RES:
            for m in rx.finditer(raw):
                if m.group(1) != "videoseries" and m.group(1) not in ids:
                    ids.append(m.group(1))
    return ids[:MAX_VIDEOS] if MAX_VIDEOS > 0 else []


def build_youtube_embed(vid, title=""):
    """Вбудований iframe YouTube (розмір завжди 840x480)."""
    return (
        "<!-- wp:html -->\n"
        f'<iframe width="840" height="480" src="https://www.youtube.com/embed/{vid}" '
        f'title="{escape(title, quote=True)}" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; '
        'picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" '
        "allowfullscreen></iframe>\n"
        "<!-- /wp:html -->"
    )


def insert_videos(content, embeds):
    """Відео ставимо одразу після другого абзацу (після блоку з джерелом);
    якщо абзаців менше, у кінець статті."""
    if not embeds:
        return content
    parts = UNIT_RE.split(content)
    out, count, done = [], 0, False
    for i, part in enumerate(parts):
        out.append(part)
        if i % 2 == 1 and part.lower().startswith("<p"):
            count += 1
            if count == 2 and not done:
                out.append("\n" + "\n".join(embeds) + "\n")
                done = True
    if not done:
        out.append("\n" + "\n".join(embeds) + "\n")
    return "".join(out)


def create_post(data, image_id, images, status, tag_ids=None, date_gmt=None, category_ids=None, videos=None, main_in_body=True, badges=None):
    """images: список (адреса фото на вашому сайті, підпис або None);
    videos: список ідентифікаторів відео YouTube."""
    figures = [build_figure(u, c, data["title"]) for u, c in images]
    content = insert_figures(data["content"], figures, has_main=main_in_body)
    content = insert_videos(content, [build_youtube_embed(v, data["title"]) for v in (videos or [])])
    payload = {
        "title": data["title"],
        "content": content,
        "excerpt": data.get("excerpt", ""),
        "status": status,
    }
    if image_id:
        payload["featured_media"] = image_id
    if tag_ids:
        payload["tags"] = tag_ids
    if category_ids:
        payload["categories"] = category_ids
    if date_gmt:
        payload["date_gmt"] = date_gmt  # час публікації як у джерела
    if badges:  # плашки на сайті (СПОРТ, АВТО, ВІДЕО): мета-поле запису, список ключів
        payload["meta"] = {BADGE_META_KEY: list(badges)}
    resp = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", auth=auth, json=payload, timeout=60)
    resp.raise_for_status()
    j = resp.json()
    if badges:
        meta_now = j.get("meta") if isinstance(j.get("meta"), dict) else {}
        # ДІАГНОСТИКА: усі мета-поля, які сайт віддає через REST, і їхні значення
        print("Мета-поля запису на сайті (REST):", {k: v for k, v in meta_now.items()} or "(порожньо: жодне поле не зареєстроване для REST)")
        saved = meta_now.get(BADGE_META_KEY)
        if not isinstance(saved, list) or set(saved) != set(badges):
            print(f"УВАГА: плашки {badges} сайт не підтвердив (отримано: {saved!r}): поле {BADGE_META_KEY} "
                  "не зареєстроване для REST (див. PHP-фрагмент) або має іншу назву")
        else:
            print("Плашки на сайті:", saved)
    return j.get("link")


def fetch_feed(feed_url):
    """Читає RSS-стрічку. Повертає (список новин, статус для звіту)."""
    entries, status = [], "?"
    try:
        r = requests.get(feed_url, headers=HEADERS, timeout=30)
        status = f"HTTP {r.status_code}"
        if r.status_code == 200:
            entries = list(feedparser.parse(r.content).entries)
    except Exception as e:
        status = f"помилка: {e}"
    if not entries:  # запасний спосіб читання
        try:
            entries = list(feedparser.parse(feed_url, agent=HEADERS["User-Agent"]).entries)
        except Exception:
            pass
    return entries, status


def collect_entries():
    """Збирає новини з усіх джерел, чергуючи їх (по одній з кожного),
    щоб одне джерело не займало весь ліміт за запуск."""
    per_feed = []
    for feed_url in FEEDS:
        entries, status = fetch_feed(feed_url)
        FEED_REPORT.append((feed_url, status, len(entries)))
        print(f"{feed_url}: {status}, новин у стрічці: {len(entries)}")
        if not entries:
            print("ПРОБЛЕМА: стрічка порожня або недоступна (можливо, сайт блокує запити з GitHub)")
        for e in entries:
            try:
                e["_feed"] = feed_url  # з якої стрічки новина (для категорій)
            except Exception:
                pass
        per_feed.append(entries[:MAX_ENTRIES])

    mixed = []
    for i in range(MAX_ENTRIES):
        for entries in per_feed:
            if i < len(entries):
                mixed.append(entries[i])
    return mixed


def write_summary(done, n_seen, n_short, errors, n_old=0, n_skip=0):
    """Короткий підсумок запуску на сторінці запуску в GitHub Actions."""
    lines = ["### Підсумок запуску", f"- Опубліковано: **{done}**"]
    for url, status, n in FEED_REPORT:
        lines.append(f"- Стрічка `{url}`: {status}, новин: {n}")
    lines.append(
        f"- Вже оброблених пропущено: {n_seen}; занадто коротких: {n_short}; "
        f"застарілих (старше {MAX_AGE_HOURS:g} год): {n_old}; пропущено за розділом (колонки тощо): {n_skip}"
    )
    if errors:
        lines.append(f"- Помилок: **{len(errors)}**. Перша: `{errors[0][:300]}`")
    if FEED_REPORT and all(n == 0 for _u, _s, n in FEED_REPORT):
        lines.append("- **ПРОБЛЕМА:** жодна стрічка не віддала новин (сайт блокує запити або стрічка змінилась)")
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def rss_text(entry):
    """Короткий текст новини з самої RSS-стрічки (запасний варіант)."""
    raw = entry.get("summary") or entry.get("description") or ""
    if not raw:
        return ""
    try:
        return _clean(lxml_html.fromstring("<div>" + raw + "</div>").text_content())
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", raw))


def entry_age_hours(entry):
    """Скільки годин тому новину опубліковано в джерелі (або None)."""
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pub:
        return None
    try:
        return max(0.0, (time.time() - calendar.timegm(pub)) / 3600)
    except Exception:
        return None


def entry_date_gmt(entry):
    """Час публікації новини в джерелі (UTC) або None."""
    pub = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pub:
        return None
    try:
        if calendar.timegm(pub) > time.time():  # дата з майбутнього: ігноруємо
            return None
        return time.strftime("%Y-%m-%dT%H:%M:%S", pub)
    except Exception:
        return None


def mark_seen_only():
    """Службовий режим: позначаємо всі поточні новини зі стрічок як оброблені,
    нічого не публікуючи (щоб не з'являлися дублі)."""
    seen = load_seen()
    added = 0
    for entry in collect_entries():
        link = entry.get("link")
        if link and link not in seen:
            seen.append(link)
            added += 1
    save_seen(seen)
    print(f"Позначено як оброблені: {added} новин. Нічого не опубліковано.")


def run_once():
    """Одна перевірка стрічок: нові новини переписуються й публікуються."""
    started = time.time()
    seen = load_seen()
    done = 0
    FEED_REPORT.clear()

    n_seen, n_short, n_old, n_skip, errors = 0, 0, 0, 0, []
    for entry in collect_entries():
        if done >= MAX_POSTS:
            break
        if time.time() - started > MAX_RUN_SECONDS:
            print("Час запуску вичерпано, решту новин оброблю наступного разу")
            break
        link = entry.get("link")
        if not link:
            continue
        if link in seen:
            n_seen += 1
            continue
        if any(is_skipped_url(u) for u in entry_urls(entry)):
            n_skip += 1
            seen.append(link)  # авторські колонки, проєкти тощо не публікуємо
            print("Пропущено (розділ, який не публікуємо):", link)
            continue
        age = entry_age_hours(entry)
        if MAX_AGE_HOURS > 0 and age is not None and age > MAX_AGE_HOURS:
            n_old += 1
            seen.append(link)  # застаріла новина: не публікуємо, щоб не було «пачок» старих
            continue

        try:
            page_ok = True
            print("Обробляю:", link)
            _FULL["on"] = is_full_caption_source(link, entry.get("_feed"))
            if _FULL["on"]:
                print("Джерело з повними підписами до фото (sport.nv.ua)")
            try:
                text, image, page_html = get_article(link)
            except SkipArticle as e:
                n_skip += 1
                seen.append(link)
                save_seen(seen)
                print("Пропущено (на сторінці розділ, який не публікуємо):", link, "->", e)
                continue
            except Exception as e:
                page_ok = False
                print("Сторінку не відкрито, беру текст з RSS:", link, "->", e)
                text, image, page_html = "", None, ""

            if len(text) < 300:  # якщо зі сторінки текст не витягнувся, беремо з RSS
                alt = rss_text(entry)
                if len(alt) > len(text):
                    text = alt
            if len(text) < MIN_TEXT:  # дуже короткий текст: додаємо заголовок новини
                text = (entry.get("title", "") + ". " + text).strip(" .")
            if len(text) < MIN_TEXT:
                print("Занадто короткий текст, пропускаю:", link)
                n_short += 1
                seen.append(link)
                save_seen(seen)
                continue

            links = find_source_links(page_html, link) if page_ok else []
            if links:
                print("Посилання на джерела в оригіналі:", [(l["anchor"], l["url"]) for l in links])
            data, status = rewrite_checked(entry.get("title", ""), text, links)

            image_id, images, main_in_body = None, [], True
            img_url = image or entry_image(entry)
            if img_url:
                image_id, wp_url = upload_image(img_url)
                rss_c = sanitize_caption(rss_caption(entry))
                page_c = sanitize_caption(
                    find_photo_caption(page_html, img_url, is_main=True)
                ) if page_html else None
                # повний підпис зі сторінки (з джерелом у дужках) має перевагу над обрізаним з RSS
                if _FULL["on"]:
                    caption, cap_src = pick_full_caption(page_c, rss_c)
                elif page_c and (not rss_c or (norm(rss_c) in norm(page_c) and len(page_c) > len(rss_c))):
                    caption, cap_src = page_c, "сторінка новини"
                else:
                    caption, cap_src = rss_c, "RSS"
                print(
                    "Підпис до головного фото:",
                    f"{caption} (знайдено: {cap_src})" if caption else f"(підпису на сайті-джерелі не знайдено, фото без підпису: {link})",
                )
                if not caption and page_html:
                    print("ДІАГНОСТИКА, HTML навколо фото:", debug_image_context(page_html, img_url))
                if main_photo_is_getty(page_html, img_url, caption, rss_c, page_c):
                    # Getty Images: фото лише на обкладинці (featured_media), у тіло статті не вставляємо
                    main_in_body = False
                    print("Головне фото від Getty Images: лише обкладинка, у тіло не вставляю:", img_url)
                elif wp_url:
                    images.append((wp_url, caption))

            # додаткові фото зі статті (фото Getty Images сюди не потрапляють)
            for extra in find_article_images(page_html, link, [img_url, image]):
                if is_getty_image(page_html, extra):
                    print("Пропущено фото Getty Images (не вставляємо в статтю):", extra)
                    continue
                _, wp_url = upload_image(extra)
                if not wp_url:
                    continue
                cap = sanitize_caption(find_photo_caption(page_html, extra))
                if _FULL["on"]:
                    cap = normalize_photo_caption(cap)
                print("Додаткове фото:", extra, "| підпис:", cap or "(не знайдено, без підпису)")
                images.append((wp_url, cap))

            video_ids = find_youtube_ids(page_html, link) if page_ok else []
            if not video_ids:
                video_ids = youtube_ids_from_entry(entry)
            if video_ids:
                print("Відео YouTube в оригіналі:", video_ids)

            tag_ids = get_tag_ids(data.get("tags"))
            print("Позначки:", clean_tags(data.get("tags")))

            cat_name = FEED_CATEGORIES.get(entry.get("_feed"))
            category_ids = []
            if cat_name:
                cid = get_category_id(cat_name)
                if cid:
                    category_ids = [cid]
                print("Категорія:", cat_name if cid else f"{cat_name} (не вдалося, без категорії)")

            badges = []
            b = CATEGORY_BADGES.get((cat_name or "").strip().upper())
            if b:
                badges.append(b)
            if video_ids:  # у новині є відео YouTube: категорії НОВИНИ + ВІДЕО і плашка ВІДЕО
                for vc in VIDEO_CATEGORIES:
                    vcid = get_category_id(vc)
                    if vcid and vcid not in category_ids:
                        category_ids.append(vcid)
                    elif not vcid:
                        print(f"Категорію {vc} не знайдено й не створено")
                if VIDEO_BADGE not in badges:
                    badges.append(VIDEO_BADGE)
                print("Є відео YouTube: категорії", VIDEO_CATEGORIES, "і плашка", VIDEO_BADGE)
            if badges:
                print("Плашки:", badges)

            post_link = create_post(
                data, image_id, images, status, tag_ids,
                entry_date_gmt(entry) if USE_SOURCE_DATE else None,
                category_ids,
                video_ids,
                main_in_body,
                badges,
            )
            print(f"Створено ({status}):", post_link)
            seen.append(link)
            save_seen(seen)  # зберігаємо одразу, щоб не було дублів при збої
            done += 1
        except Exception as e:
            print("Помилка з новиною", link, "->", e)
            errors.append(f"{link} -> {e}")

    _FULL["on"] = False
    save_seen(seen)
    print(f"Готово. Нових матеріалів: {done}")
    write_summary(done, n_seen, n_short, errors, n_old, n_skip)


def commit_state():
    """У GitHub Actions: зберігаємо seen.json у репозиторії між перевірками."""
    if os.environ.get("GITHUB_ACTIONS") and os.path.exists("save_seen.py"):
        try:
            subprocess.run([sys.executable, "save_seen.py"], timeout=180)
        except Exception as e:
            print("Не вдалося зберегти список оброблених новин:", e)


def main():
    if MARK_SEEN_ONLY:
        mark_seen_only()
        return
    start = time.time()
    while True:
        run_once()
        if RUN_LOOP_MINUTES <= 0:
            break
        commit_state()
        remaining = RUN_LOOP_MINUTES * 60 - (time.time() - start)
        if remaining < POLL_SECONDS + 120:
            break
        print(f"Наступна перевірка через {POLL_SECONDS // 60} хв...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
