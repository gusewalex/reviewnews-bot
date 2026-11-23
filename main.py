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
    # Щоб додати ще джерело, додайте новий рядок з RSS-посиланням
]

# У яку категорію WordPress публікувати новини з певної стрічки.
# Стрічки, яких тут немає, публікуються без вказаної категорії (як раніше).
FEED_CATEGORIES = {
    "https://nv.ua/ukr/rss/2371.xml": "СПОРТ",
}

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
# сайти, на які бот не робить посилань і назви яких прибирає з тексту
BLOCK_LINK_HOSTS = [
    x.strip().lower()
    for x in (os.environ.get("BLOCK_LINK_HOSTS") or "eurointegration.com.ua,epravda.com.ua,nv.ua").split(",")
    if x.strip()
]
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
    if len(t) <= 200:
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
    """True, якщо фрагмент підпису це сайт/домен або назва сайту-джерела."""
    return bool(DOMAIN_RE.search(text)) or has_site_name(text)


def sanitize_caption(caption):
    """Прибирає з підпису сайти як джерела.
    'Фото: pravda.com.ua' -> None;
    'Фото: Запорізька ОВА / pravda.com.ua' -> 'Фото: Запорізька ОВА';
    'наслідки атаки, фото ОВА' -> без змін;
    'наслідки атаки, фото pravda.com.ua' -> 'наслідки атаки'."""
    caption = _clean(caption)
    if not caption:
        return None
    prefix, sep, rest = caption.partition(":")
    if not sep:
        if not is_site_part(caption):
            return caption
        names = "|".join(re.escape(n) for n in SITE_NAMES)
        cleaned = re.sub(
            r"[,.;]?\s*(?:ілюстративне\s+)?(?:фото|скріншот)\s*:?\s*[«\"]?(?:"
            + DOMAIN_RE.pattern + "|" + names + r")[»\"]?",
            "", caption, flags=re.I,
        )
        cleaned = _clean(cleaned).strip(" ,;.")
        return None if (not cleaned or is_site_part(cleaned)) else cleaned
    rest2 = DOMAIN_RE.sub(" / ", rest)  # адреси й домени вирізаємо одразу
    parts = [x.strip() for x in re.split(r"\s*[/|;,]\s*", rest2) if x.strip()]
    good = [x for x in parts if not has_site_name(x)]
    if not good:
        return None
    if rest2 == rest and len(good) == len(parts):
        return caption
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
    return _clean(" ".join(el.itertext()))


def looks_like_caption(t):
    """Короткий текст, схожий на підпис під фото (є вказівка «фото ...», «скріншот: ...»)."""
    t = _clean(t)
    if not t or len(t) > 250:
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
        if parent.tag == "figure":
            fc = parent.find(".//figcaption")
            if fc is not None:
                c = _caption_from_text(_text(fc))
                if c:
                    return c
        for el in parent.iter():
            if el is img or not isinstance(el.tag, str):
                continue
            cls = (el.get("class") or "") + " " + (el.get("id") or "")
            if el.tag == "figcaption" or CAPTION_HINT.search(cls):
                c = _caption_from_text(_text(el))
                if c:
                    return c
        t = _text(parent)
        if (
            t
            and len(t) <= 300
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
    name = (image_url or "").split("?")[0].rstrip("/").split("/")[-1]
    if len(name) < 5:
        return []
    return [
        img for img in doc.iter("img")
        if name in " ".join(img.get(a) or "" for a in IMG_ATTRS)
    ]


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
    return None


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
            if isins
