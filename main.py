import calendar
import json
import os
import re
import time
from html import escape
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import trafilatura
from lxml import html as lxml_html
from openai import OpenAI

# ---------- Налаштування ----------
FEEDS = [
    "https://www.pravda.com.ua/rss/view_news/",
    # Щоб додати ще джерело, додайте новий рядок з RSS-посиланням
]

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

POST_STATUS = os.environ.get("POST_STATUS", "draft")  # draft або publish
MAX_POSTS = int(os.environ.get("MAX_POSTS", "30"))     # скільки новин за один запуск
MAX_ENTRIES = int(os.environ.get("MAX_ENTRIES", "60")) # скільки останніх новин читати з кожної стрічки
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "1200"))  # після цього нові новини не беруться
MIN_TEXT = 120                                         # мінімальна довжина тексту новини
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "4"))    # скільки додаткових фото з новини
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SEEN_FILE = "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ReviewNewsBot/1.0)"}

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
4. Прямі цитати (текст у лапках «...» або "..."): копіюй дослівно, символ
   у символ, не перефразовуй і не скорочуй. Обов'язково вказуй, хто це сказав.
   Непрямі висловлювання перефразовуй вільно.
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


def get_article(url):
    """Повертає (текст, посилання на картинку, HTML сторінки)."""
    html = requests.get(url, headers=HEADERS, timeout=30).text
    text = trafilatura.extract(html) or ""
    meta = trafilatura.extract_metadata(html)
    image = meta.image if meta and meta.image else None
    return text, image, html


# ---------- Підпис до фото (як у новині-першоджерелі) ----------
CAPTION_HINT = re.compile(
    r"caption|credit|photo-?(?:source|author|desc)|img-?(?:source|author|desc)"
    r"|image-?(?:source|author|desc)|source-?photo",
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


def is_site_part(text):
    """True, якщо фрагмент підпису це сайт/домен або назва сайту-джерела."""
    low = text.lower()
    return bool(DOMAIN_RE.search(text)) or any(n in low for n in SITE_NAMES)


def sanitize_caption(caption):
    """Прибирає з підпису сайти як джерела.
    'Фото: pravda.com.ua' -> None;
    'Фото: Запорізька ОВА / pravda.com.ua' -> 'Фото: Запорізька ОВА'."""
    if not caption:
        return None
    prefix, sep, rest = caption.partition(":")
    if not sep:
        return None if is_site_part(caption) else caption
    rest2 = DOMAIN_RE.sub(" / ", rest)  # адреси й домени вирізаємо одразу
    parts = [x.strip() for x in re.split(r"\s*[/|;,]\s*", rest2) if x.strip()]
    good = [x for x in parts if not any(n in x.lower() for n in SITE_NAMES)]
    if not good:
        return None
    if rest2 == rest and len(good) == len(parts):
        return caption
    return f"{prefix.strip()}: {', '.join(good)}"


def find_photo_caption(html_text, image_url):
    """Шукає на сторінці новини підпис до головного фото
    (наприклад, 'Фото: Getty Images'). Повертає текст або None."""
    if not image_url or not html_text:
        return None
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return None

    # 1) Знаходимо саме це зображення на сторінці й беремо його підпис
    name = image_url.split("?")[0].rstrip("/").split("/")[-1]
    if len(name) >= 5:
        for img in doc.iter("img"):
            attrs = " ".join(
                img.get(a) or "" for a in ("src", "data-src", "data-lazy-src", "srcset", "data-srcset")
            )
            if name not in attrs:
                continue
            node = img
            for _ in range(4):  # піднімаємось на кілька рівнів угору
                node = node.getparent()
                if node is None:
                    break
                if node.tag == "figure":
                    fc = node.find(".//figcaption")
                    if fc is not None:
                        c = _caption_from_text(fc.text_content())
                        if c:
                            return c
                for el in node.iter():
                    if el is img or not isinstance(el.tag, str):
                        continue
                    cls = (el.get("class") or "") + " " + (el.get("id") or "")
                    if el.tag == "figcaption" or CAPTION_HINT.search(cls):
                        c = _caption_from_text(el.text_content())
                        if c:
                            return c
                m = CREDIT_RE.search(_clean(node.text_content()))
                if m and len(_clean(node.text_content())) <= 400:
                    return _clean(m.group(0))

    # 2) Запасний варіант: перший підпис 'Фото: ...' у статті
    scope = doc.xpath("//article") or [doc]
    for el in scope[0].iter():
        if not isinstance(el.tag, str):
            continue
        cls = (el.get("class") or "") + " " + (el.get("id") or "")
        if el.tag == "figcaption" or CAPTION_HINT.search(cls):
            t = _clean(el.text_content())
            if t and len(t) <= 200 and CREDIT_RE.search(t):
                return t
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
QUOTE_RE = re.compile(
    r"«([^»]{15,})»|„([^“”]{15,})[“”]|“([^”]{15,})”|\"([^\"]{15,})\""
)


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


def rewrite_checked(title, text):
    """Переписує новину й перевіряє, що прямі цитати збережені дослівно.
    Повертає (дані, статус_публікації)."""
    data = rewrite(title, text)
    miss = missing_quotes(text, data["content"])
    if not miss:
        return data, POST_STATUS

    print("Цитати змінено, пробую ще раз:", miss)
    extra = (
        "У попередній спробі ці прямі цитати були змінені. "
        "Вони мають бути в тексті ДОСЛІВНО, без жодних змін:\n- "
        + "\n- ".join(miss)
    )
    data = rewrite(title, text, extra)
    miss = missing_quotes(text, data["content"])
    if not miss:
        return data, POST_STATUS

    # Не вдалося зберегти цитати: залишаємо як чернетку для ручної перевірки
    print("Цитати все ще не збігаються, зберігаю як чернетку:", miss)
    return data, "draft"


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


DEFAULT_CAPTION = "Фото з відкритих джерел"  # якщо в джерелі підпису немає


def build_figure(image_url, caption, alt):
    caption = caption or DEFAULT_CAPTION  # підпис є завжди
    figcaption = f"<figcaption>{escape(caption)}</figcaption>"
    return (
        '<figure class="wp-block-image size-large">'
        f'<img src="{escape(image_url)}" alt="{escape(alt)}"/>'
        f"{figcaption}"
        "</figure>"
    )


def insert_figures(content, figures):
    """Розставляє фото по тексту: перше після 1-го абзацу, далі після 3-го,
    5-го, 7-го... Якщо абзаців менше, решта йде в кінець статті."""
    if not figures:
        return content
    parts = content.split("</p>")
    paragraphs = [p + "</p>" for p in parts[:-1]]
    tail = parts[-1]
    if not paragraphs:
        return "\n".join(figures) + "\n" + content

    slots = [1, 3, 5, 7, 9, 11]
    fig_at = {}
    for i, fig in enumerate(figures):
        n = slots[i] if i < len(slots) else len(paragraphs)
        fig_at.setdefault(min(n, len(paragraphs)), []).append(fig)

    out = []
    for i, para in enumerate(paragraphs, 1):
        out.append(para)
        out.extend(fig_at.get(i, []))
    return "\n".join(out) + tail


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


def create_post(data, image_id, images, status, tag_ids=None, date_gmt=None):
    """images: список (адреса фото на вашому сайті, підпис або None)."""
    figures = [build_figure(u, c, data["title"]) for u, c in images]
    content = insert_figures(data["content"], figures)
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
    if date_gmt:
        payload["date_gmt"] = date_gmt  # час публікації як у джерела
    resp = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", auth=auth, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json().get("link")


def collect_entries():
    """Збирає новини з усіх джерел, чергуючи їх (по одній з кожного),
    щоб одне джерело не займало весь ліміт за запуск."""
    per_feed = []
    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
            per_feed.append(list(feed.entries[:MAX_ENTRIES]))
            print(f"{feed_url}: знайдено {len(feed.entries)} новин")
        except Exception as e:
            print("Не вдалося прочитати стрічку", feed_url, "->", e)

    mixed = []
    for i in range(MAX_ENTRIES):
        for entries in per_feed:
            if i < len(entries):
                mixed.append(entries[i])
    return mixed


def rss_text(entry):
    """Короткий текст новини з самої RSS-стрічки (запасний варіант)."""
    raw = entry.get("summary") or entry.get("description") or ""
    if not raw:
        return ""
    try:
        return _clean(lxml_html.fromstring("<div>" + raw + "</div>").text_content())
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", raw))


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


def main():
    started = time.time()
    seen = load_seen()
    done = 0

    for entry in collect_entries():
        if done >= MAX_POSTS:
            break
        if time.time() - started > MAX_RUN_SECONDS:
            print("Час запуску вичерпано, решту новин оброблю наступного разу")
            break
        link = entry.get("link")
        if not link or link in seen:
            continue

        try:
            page_ok = True
            try:
                text, image, page_html = get_article(link)
            except Exception as e:
                page_ok = False
                print("Сторінку не відкрито, беру текст з RSS:", link, "->", e)
                text, image, page_html = "", None, ""

            if len(text) < 300:  # якщо зі сторінки текст не витягнувся, беремо з RSS
                alt = rss_text(entry)
                if len(alt) > len(text):
                    text = alt
            if len(text) < MIN_TEXT:
                print("Занадто короткий текст, пропускаю:", link)
                if page_ok:
                    seen.append(link)
                    save_seen(seen)
                continue

            data, status = rewrite_checked(entry.get("title", ""), text)

            image_id, images = None, []
            img_url = image or entry_image(entry)
            if img_url:
                image_id, wp_url = upload_image(img_url)
                caption = sanitize_caption(find_photo_caption(page_html, img_url))
                print("Підпис до головного фото:", caption or f"({DEFAULT_CAPTION})")
                if wp_url:
                    images.append((wp_url, caption))

            # додаткові фото зі статті
            for extra in find_article_images(page_html, link, [img_url, image]):
                _, wp_url = upload_image(extra)
                if not wp_url:
                    continue
                cap = sanitize_caption(find_photo_caption(page_html, extra))
                print("Додаткове фото:", extra, "| підпис:", cap or f"({DEFAULT_CAPTION})")
                images.append((wp_url, cap))

            tag_ids = get_tag_ids(data.get("tags"))
            print("Позначки:", clean_tags(data.get("tags")))

            post_link = create_post(
                data, image_id, images, status, tag_ids, entry_date_gmt(entry)
            )
            print(f"Створено ({status}):", post_link)
            seen.append(link)
            save_seen(seen)  # зберігаємо одразу, щоб не було дублів при збої
            done += 1
        except Exception as e:
            print("Помилка з новиною", link, "->", e)

    save_seen(seen)
    print(f"Готово. Нових матеріалів: {done}")


if __name__ == "__main__":
    main()
