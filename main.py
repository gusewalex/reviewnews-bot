import json
import os
import re
from html import escape
from urllib.parse import urlparse

import feedparser
import requests
import trafilatura
from lxml import html as lxml_html
from openai import OpenAI

# ---------- Налаштування ----------
FEEDS = [
    "https://www.pravda.com.ua/rss/view_news/",
    "https://24tv.ua/rss/all.xml?googleFlg=true&tagId=1117",
    # Щоб додати ще джерело, додайте новий рядок з RSS-посиланням
]

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

POST_STATUS = os.environ.get("POST_STATUS", "draft")  # draft або publish
MAX_POSTS = int(os.environ.get("MAX_POSTS", "3"))      # скільки новин за один запуск
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
   з оригіналу (крім цитат). Обсяг орієнтовно такий самий, як в оригіналі,
   але не менше 3 абзаців.
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


def add_image_to_content(content, image_url, caption, alt):
    """Вставляє фото з підписом у текст статті, після першого абзацу."""
    figcaption = f"<figcaption>{escape(caption)}</figcaption>" if caption else ""
    figure = (
        '<figure class="wp-block-image size-large">'
        f'<img src="{escape(image_url)}" alt="{escape(alt)}"/>'
        f"{figcaption}"
        "</figure>"
    )
    idx = content.find("</p>")
    if idx == -1:
        return figure + "\n" + content
    idx += len("</p>")
    return content[:idx] + "\n" + figure + "\n" + content[idx:]


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


def create_post(data, image_id, image_url, caption, status, tag_ids=None):
    content = data["content"]
    if image_url:
        content = add_image_to_content(content, image_url, caption, data["title"])
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
            per_feed.append(list(feed.entries[:15]))
            print(f"{feed_url}: знайдено {len(feed.entries)} новин")
        except Exception as e:
            print("Не вдалося прочитати стрічку", feed_url, "->", e)

    mixed = []
    for i in range(15):
        for entries in per_feed:
            if i < len(entries):
                mixed.append(entries[i])
    return mixed


def main():
    seen = load_seen()
    done = 0

    for entry in collect_entries():
        if done >= MAX_POSTS:
            break
        link = entry.get("link")
        if not link or link in seen:
            continue

        try:
            text, image, page_html = get_article(link)
            if len(text) < 300:
                print("Занадто короткий текст, пропускаю:", link)
                seen.append(link)
                continue

            data, status = rewrite_checked(entry.get("title", ""), text)
            image_id, wp_image_url, caption = None, None, None
            img_url = image or entry_image(entry)
            if img_url:
                image_id, wp_image_url = upload_image(img_url)
                caption = sanitize_caption(find_photo_caption(page_html, img_url))
                print("Підпис до фото:", caption or "(не знайдено, без підпису)")

            tag_ids = get_tag_ids(data.get("tags"))
            print("Позначки:", clean_tags(data.get("tags")))

            post_link = create_post(
                data, image_id, wp_image_url, caption, status, tag_ids
            )
            print(f"Створено ({status}):", post_link)
            seen.append(link)
            done += 1
        except Exception as e:
            print("Помилка з новиною", link, "->", e)

    save_seen(seen)
    print(f"Готово. Нових матеріалів: {done}")


if __name__ == "__main__":
    main()
