import os
import json
import time
import random
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================
# CONFIG
# =========================

@dataclass(frozen=True)
class Config:
    base_url: str = "https://chromehearts.com"
    slugs_to_watch: tuple = (
        "t-shirt", "hoodie", "hat", "eyewear", "slides", "slippers", "winter",
        "shoes", "shirt", "silichrome", "rib-tank", "sweater", "goggles",
        "gloves", "beanie",
    )

    # Interval between full sweeps of all slugs
    check_interval_sec: int = 3600  # 1 hour

    # Small random jitter per request to look less bot-like
    per_request_jitter_sec: tuple = (0.4, 1.2)

    # Persistence
    state_file: str = "watch_state.json"

    # Telegram (set these as env vars)
    tg_bot_token: str = os.getenv("TG_BOT_TOKEN", "")
    tg_chat_id: str = os.getenv("TG_CHAT_ID", "")

    # Request behavior
    timeout_sec: int = 15
    treat_non_homepage_as_live: bool = True  # If False, adds stricter heuristics


CFG = Config()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("dropwatch")


# =========================
# STATE
# =========================

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Failed to load state file (%s). Starting fresh. Error: %s", path, e)
        return {}

def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# =========================
# HTTP SESSION (RETRIES)
# =========================

def build_session() -> requests.Session:
    s = requests.Session()

    retries = Retry(
        total=4,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    s.headers.update(HEADERS)
    return s


# =========================
# URL HELPERS
# =========================

def normalize_host(netloc: str) -> str:
    return netloc.lower().lstrip("www.")

def is_homepage(url: str) -> bool:
    """
    True if url is effectively the site root, ignoring scheme, www, trailing slashes, and query params.
    """
    if not url:
        return True
    p = urlparse(url)
    host = normalize_host(p.netloc)
    path = (p.path or "").rstrip("/")
    return host == "chromehearts.com" and path == ""

def same_domain(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url)
    return normalize_host(p.netloc) == "chromehearts.com"


# =========================
# TELEGRAM (NO EXTRA LIBS)
# =========================

def send_telegram(text: str) -> None:
    if not CFG.tg_bot_token or not CFG.tg_chat_id:
        log.error("Missing TG_BOT_TOKEN or TG_CHAT_ID env vars; can't send Telegram message.")
        return

    api = f"https://api.telegram.org/bot{CFG.tg_bot_token}/sendMessage"
    payload = {
        "chat_id": CFG.tg_chat_id,
        "text": text,
        "disable_web_page_preview": False,
    }

    try:
        r = requests.post(api, data=payload, timeout=CFG.timeout_sec)
        if r.status_code != 200:
            log.error("Telegram send failed (%s): %s", r.status_code, r.text[:300])
        else:
            log.info("Telegram sent.")
    except requests.RequestException as e:
        log.error("Telegram network error: %s", e)


# =========================
# DROP CHECK
# =========================

def check_slug(session: requests.Session, slug: str) -> tuple[str, str | None]:
    """
    Returns (state, final_url)
      state in {"dead","live","error"}
    """
    slug = slug.strip().lstrip("/")
    url = f"{CFG.base_url}/{slug}"

    try:
        resp = session.get(url, allow_redirects=True, timeout=CFG.timeout_sec)
        final_url = resp.url

        # If it ends at homepage => dead
        if is_homepage(final_url):
            return "dead", final_url

        # If it ends off-domain, treat carefully (could be blocking / WAF / etc.)
        if not same_domain(final_url):
            log.warning("Slug %s redirected off-domain: %s", slug, final_url)
            # Usually treat as error rather than "live"
            return "error", final_url

        if CFG.treat_non_homepage_as_live:
            return "live", final_url

        # Stricter heuristic (optional):
        # Only live if final path isn't just "/<slug>" or "/" (and response looks like a product page)
        p = urlparse(final_url)
        final_path = (p.path or "").rstrip("/")
        if final_path and final_path != f"/{slug}":
            return "live", final_url

        return "dead", final_url

    except requests.RequestException as e:
        log.error("Network error checking %s: %s", slug, e)
        return "error", None


# =========================
# MAIN LOOP
# =========================

def main():
    session = build_session()
    state = load_state(CFG.state_file)

    # Ensure all slugs exist in state
    for slug in CFG.slugs_to_watch:
        state.setdefault(slug, "dead")

    save_state(CFG.state_file, state)

    log.info("Watching %d slugs. Interval: %ds", len(CFG.slugs_to_watch), CFG.check_interval_sec)

    while True:
        for slug in CFG.slugs_to_watch:
            # polite jitter between requests
            time.sleep(random.uniform(*CFG.per_request_jitter_sec))

            current, final_url = check_slug(session, slug)
            previous = state.get(slug, "dead")

            if current == "live" and previous != "live":
                msg = (
                    f"New drop detected ✅\n\n"
                    f"Item: {slug.upper()}\n"
                    f"Link: {final_url}\n"
                )
                send_telegram(msg)
                state[slug] = "live"
                save_state(CFG.state_file, state)

            elif current == "dead" and previous == "live":
                log.info("%s is back to dead.", slug)
                state[slug] = "dead"
                save_state(CFG.state_file, state)

            log.info("[%s] %s -> %s", slug, previous.upper(), current.upper())

        log.info("Sweep complete. Sleeping %ds...", CFG.check_interval_sec)
        time.sleep(CFG.check_interval_sec)


if __name__ == "__main__":
    main()