import json
import os
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

PROMPT = """Ти редактор новинного сайту. Перепиши новину українською мовою
своїми словами: збережи всі факти, імена, цифри й дати, але повністю зміни
формулювання та структуру. Нічого не вигадуй і не додавай від себе.
Не копіюй речення з оригіналу.

Поверни JSON з полями:
- "title": новий короткий заголовок (до 90 символів),
- "content": текст новини у HTML (абзаци в <p>, 3-6 абзаців),
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


def rewrite(title, text):
    resp = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"Заголовок: {title}\n\nТекст:\n{text[:8000]}"},
        ],
    )
    return json.loads(resp.choices[0].message.content)


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


def create_post(data, source_url, image_id):
    domain = urlparse(source_url).netloc
    content = (
        data["content"]
        + f'\n<p><em>Джерело: <a href="{source_url}" target="_blank" '
        f'rel="nofollow noopener">{domain}</a></em></p>'
    )
    payload = {
        "title": data["title"],
        "content": content,
        "excerpt": data.get("excerpt", ""),
        "status": POST_STATUS,
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

                data = rewrite(entry.get("title", ""), text)
                image_id = None
                img_url = image or entry_image(entry)
                if img_url:
                    image_id = upload_image(img_url)

                post_link = create_post(data, link, image_id)
                print("Створено:", post_link)
                seen.append(link)
                done += 1
            except Exception as e:
                print("Помилка з новиною", link, "->", e)

    save_seen(seen)
    print(f"Готово. Нових матеріалів: {done}")


if __name__ == "__main__":
    main()
