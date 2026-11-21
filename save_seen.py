"""Зберігає seen.json у репозиторії, об'єднуючи його з версією на GitHub.
Конфліктів не буває: списки просто складаються докупи."""
import json
import subprocess
import time


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def main():
    mine = load("seen.json")  # те, що накопичив бот у цьому запуску
    git("config", "user.name", "news-bot")
    git("config", "user.email", "news-bot@users.noreply.github.com")

    for attempt in range(1, 6):
        git("fetch", "origin", "main")
        git("reset", "--hard", "origin/main")
        merged = list(dict.fromkeys(load("seen.json") + mine))[-2000:]
        with open("seen.json", "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        git("add", "seen.json")
        if git("diff", "--staged", "--quiet", check=False).returncode == 0:
            print("seen.json: змін немає")
            return
        git("commit", "-m", "update seen list")
        if git("push", "origin", "HEAD:main", check=False).returncode == 0:
            print(f"seen.json збережено (спроба {attempt})")
            return
        time.sleep(5)
    print("seen.json: не вдалося зберегти після 5 спроб")


if __name__ == "__main__":
    main()
