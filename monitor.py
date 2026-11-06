
import os
import json
import re
import html
import requests

from datetime import datetime, timezone
from google.oauth2 import service_account
from google.auth.transport.requests import Request

BLOGGER_FEED = (
    "https://www.odiastudyroom.in/feeds/posts/default"
    "?alt=json&max-results=50&orderby=published"
)

FIREBASE_PROJECT_ID = "odia-study-room-job-updates"
FCM_URL = (
    f"https://fcm.googleapis.com/v1/projects/"
    f"{FIREBASE_PROJECT_ID}/messages:send"
)

STATE_FILE = "seen_posts.json"

ALLOWED_CATEGORIES = {
    "latest jobs",
    "admit card",
    "exam date",
    "results",
    "upcoming jobs"
}


def clean_html(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def get_category(entry):
    labels = [
        item.get("term", "").strip().lower()
        for item in entry.get("category", [])
    ]

    mapping = [
        ("admit card", "Admit Card"),
        ("admit cards", "Admit Card"),
        ("admit card out", "Admit Card"),
        ("hall ticket", "Admit Card"),
        ("exam date", "Exam Date"),
        ("exam dates", "Exam Date"),
        ("exam date out", "Exam Date"),
        ("exam schedule", "Exam Date"),
        ("result", "Results"),
        ("results", "Results"),
        ("result out", "Results"),
        ("merit list", "Results"),
        ("upcoming job", "Upcoming Jobs"),
        ("upcoming jobs", "Upcoming Jobs"),
        ("upcoming recruitment", "Upcoming Jobs"),
        ("upcoming vacancy", "Upcoming Jobs"),
        ("latest job", "Latest Jobs"),
        ("latest jobs", "Latest Jobs"),
        ("recruitment", "Latest Jobs"),
        ("government jobs", "Latest Jobs"),
        ("govt jobs", "Latest Jobs"),
        ("job notification", "Latest Jobs")
    ]

    for label, category in mapping:
        if label in labels:
            return category

    return None


def load_seen_posts():
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return set(json.load(file))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_posts(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen), file, indent=2)


def get_posts():
    response = requests.get(BLOGGER_FEED, timeout=30)
    response.raise_for_status()

    feed = response.json().get("feed", {})
    entries = feed.get("entry", [])

    posts = []

    for entry in entries:
        category = get_category(entry)

        if category is None:
            continue

        title = clean_html(
            entry.get("title", {}).get("$t", "")
        )

        links = entry.get("link", [])
        url = next(
            (
                link.get("href")
                for link in links
                if link.get("rel") == "alternate"
                and link.get("type") == "text/html"
            ),
            None
        )

        if not title or not url:
            continue

        if not url.startswith("https://www.odiastudyroom.in/"):
            continue

        published = entry.get("published", {}).get("$t", "")

        content = (
            entry.get("summary", {}).get("$t")
            or entry.get("content", {}).get("$t", "")
        )

        summary = clean_html(content)[:500]

        posts.append({
            "id": url,
            "title": title,
            "url": url,
            "date": published,
            "category": category,
            "summary": summary
        })

    return posts


def get_access_token():
    secret = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not secret:
        raise RuntimeError(
            "Missing FIREBASE_SERVICE_ACCOUNT_JSON GitHub secret"
        )

    service_account_info = json.loads(secret)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            "https://www.googleapis.com/auth/firebase.messaging"
        ]
    )

    credentials.refresh(Request())
    return credentials.token


def send_notification(post, access_token):
    summary = post["summary"] or "Open the app to read the full notification."

    body = summary[:180]

    message = {
        "message": {
            "topic": "all_job_alerts",
            "data": {
                "title": post["title"],
                "body": body,
                "url": post["url"],
                "date": post["date"],
                "category": post["category"],
                "summary": summary
            },
            "android": {
                "priority": "HIGH"
            }
        }
    }

    response = requests.post(
        FCM_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json=message,
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"FCM error {response.status_code}: {response.text}"
        )

    print("Notification sent:", post["title"])


def main():
    print("Checking Blogger for new job notifications...")

    posts = get_posts()
    print("Eligible posts found:", len(posts))

    seen = load_seen_posts()

    # First run: save existing posts without sending old alerts.
    if seen is None:
        save_seen_posts({post["id"] for post in posts})
        print("Initial setup completed. Existing posts saved.")
        return

    new_posts = [
        post for post in reversed(posts)
        if post["id"] not in seen
    ]

    if not new_posts:
        print("No new job notifications.")
        return

    access_token = get_access_token()

    for post in new_posts:
        send_notification(post, access_token)
        seen.add(post["id"])

    save_seen_posts(seen)

    print("Completed:", len(new_posts), "new notifications sent.")


if __name__ == "__main__":
    main()
