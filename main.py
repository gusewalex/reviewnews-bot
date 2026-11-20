import json
import os
import re
from urllib.parse import urlparse

import feedparser
import requests
import trafilatura
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

Поверни JSON з полями:
- "title": новий інформативний заголовок до 90 символів, без клікбейту і
  без лапок-ялинок на початку,
- "content": текст новини у HTML (кожен абзац у <p>),
- "excerpt": короткий опис в 1-2 речення.
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
    """Повертає (текст, посилання на картинку) зі сторінки новини."""
    html = requests.get(url, headers=HEADERS, timeout=30).text
    text = trafilatura.extract(html) or ""
    meta = trafilatura.extract_metadata(html)
    image = meta.image if meta and meta.image else None
    return text, image


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
    """Завантажує картинку у WordPress, повертає її ID (або None)."""
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
        return resp.json()["id"]
    except Exception as e:
        print("Картинку не завантажено:", e)
        return None


def create_post(data, source_url, image_id, status):
    content = data["content"]
    payload = {
        "title": data["title"],
        "content": content,
        "excerpt": data.get("excerpt", ""),
        "status": status,
    }
    if image_id:
        payload["featured_media"] = image_id
    resp = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", auth=auth, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json().get("link")


def main():
    seen = load_seen()
    done = 0

    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url, agent=HEADERS["User-Agent"])
        for entry in feed.entries[:15]:
            if done >= MAX_POSTS:
                break
            link = entry.get("link")
            if not link or link in seen:
                continue

            try:
                text, image = get_article(link)
                if len(text) < 300:
                    print("Занадто короткий текст, пропускаю:", link)
                    seen.append(link)
                    continue

                data, status = rewrite_checked(entry.get("title", ""), text)
                image_id = None
                img_url = image or entry_image(entry)
                if img_url:
                    image_id = upload_image(img_url)

                post_link = create_post(data, link, image_id, status)
                print(f"Створено ({status}):", post_link)
                seen.append(link)
                done += 1
            except Exception as e:
                print("Помилка з новиною", link, "->", e)

    save_seen(seen)
    print(f"Готово. Нових матеріалів: {done}")


if __name__ == "__main__":
    main()
