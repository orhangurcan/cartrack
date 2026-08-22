#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

API_URL = "https://api.encar.com/search/car/list/general"
STATE_FILE = Path("state/state.json")
DETAIL_URL = "https://fem.encar.com/cars/detail/{car_id}?listAdvType=share"
BERLIN = ZoneInfo("Europe/Berlin")
PAGE_SIZE = 100
MAX_PAGES = 20
LEGACY_PRICE_STEP_10K_KRW = 100
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "ko-KR,ko;q=0.9,en;q=0.8",
    "origin": "https://www.encar.com",
    "referer": "https://www.encar.com/",
    "user-agent": USER_AGENT,
}
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "ko-KR,ko;q=0.9,en;q=0.8",
    "user-agent": USER_AGENT,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("runner")


def now_iso():
    return datetime.now(BERLIN).isoformat(timespec="seconds")


def default_state():
    return {"version": 2, "searches": {}, "startup_notified": False, "last_daily_summary_date": None}


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    with STATE_FILE.open("r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("version") != 2 or not isinstance(state.get("searches"), dict):
        raise RuntimeError("Unsupported state format")
    state.setdefault("startup_notified", False)
    state.setdefault("last_daily_summary_date", None)
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(STATE_FILE)


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def decode_search_url(search_url, price_cap_10k_krw=None):
    parsed = urlparse(search_url)
    if parsed.scheme != "https" or parsed.netloc not in {"car.encar.com", "www.encar.com"}:
        raise ValueError("Unexpected search URL host")
    raw = parse_qs(parsed.query).get("search")
    if not raw:
        raise ValueError("Search URL has no search parameter")
    payload = json.loads(raw[0])
    action = payload.get("action")
    sort = payload.get("sort") or "MobileModifiedDate"
    if not action:
        raise ValueError("Search payload has no action")

    if price_cap_10k_krw is not None:
        cap = int(price_cap_10k_krw)
        if cap <= 0:
            raise ValueError("price_cap_10k_krw must be positive")
        action, replacements = re.subn(
            r"Price\.range\(\.\.\d+\)",
            f"Price.range(..{cap})",
            action,
        )
        if replacements == 0:
            raise ValueError("Configured price cap but search URL has no upper Price.range")
    return action, sort


def legacy_price_cap(search_url):
    action, _ = decode_search_url(search_url, None)
    match = re.search(r"Price\.range\(\.\.(\d+)\)", action)
    if not match:
        raise RuntimeError("Legacy search URL has no upper Price.range")
    return int(match.group(1)) + LEGACY_PRICE_STEP_10K_KRW


def load_config(required=True):
    raw = os.getenv("SEARCHES_JSON")
    if not raw:
        if required:
            raise RuntimeError("Missing required secret: SEARCHES_JSON")
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SEARCHES_JSON is invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("SEARCHES_JSON must be a JSON object")
    try:
        format_version = int(data.get("format_version", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("format_version must be an integer") from exc
    if format_version < 1:
        raise RuntimeError("format_version must be >= 1")

    searches = data.get("searches")
    if not isinstance(searches, list) or not searches:
        raise RuntimeError("SEARCHES_JSON must contain a non-empty searches list")

    seen_keys = set()
    normalized = []
    for row in searches:
        if not isinstance(row, dict):
            raise RuntimeError("Every search config must be an object")
        for field in ("key", "label", "search_url"):
            if not row.get(field):
                raise RuntimeError(f"Search config missing {field}")
        if row["key"] in seen_keys:
            raise RuntimeError("Duplicate search key")
        seen_keys.add(row["key"])

        item = dict(row)
        # v1 exists only for backward compatibility with an already-running
        # installation. New/fork configs should use format_version 2, where
        # the Encar URL is preserved unless an explicit override is supplied.
        if format_version == 1:
            effective_cap = legacy_price_cap(item["search_url"])
        else:
            effective_cap = item.get("price_cap_10k_krw")
            if effective_cap is not None:
                try:
                    effective_cap = int(effective_cap)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("price_cap_10k_krw must be an integer or null") from exc
        item["_effective_price_cap_10k_krw"] = effective_cap
        decode_search_url(item["search_url"], effective_cap)
        normalized.append(item)

    result = dict(data)
    result["format_version"] = format_version
    result["searches"] = normalized
    return result


def state_slot(index):
    return f"s{index}"


def digest_id(car_id):
    key = require_env("STATE_KEY").encode("utf-8")
    return hmac.new(key, str(car_id).encode("utf-8"), hashlib.sha256).hexdigest()


def bootstrap(session):
    for url in ("https://www.encar.com/", "https://car.encar.com/"):
        try:
            r = session.get(url, headers=PAGE_HEADERS, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                log.info("Session bootstrap OK")
                return
        except Exception:
            pass
    log.warning("Session bootstrap did not return HTTP 200; API retries will decide final status")


def api_get(session, params, retries=3):
    delays = (5, 10, 20)
    last_exc = None
    for attempt in range(retries):
        try:
            r = session.get(API_URL, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, dict):
                    raise RuntimeError("Search API returned invalid JSON")
                return data
            last_exc = RuntimeError(f"Search API HTTP {r.status_code}")
        except Exception as exc:
            last_exc = exc
        if attempt < retries - 1:
            log.warning("Search request failed; retry %d/%d", attempt + 1, retries)
            time.sleep(delays[attempt])
    raise RuntimeError(f"Search request failed after {retries} attempts: {last_exc}")


def fetch_search(session, cfg):
    action, sort = decode_search_url(cfg["search_url"], cfg.get("_effective_price_cap_10k_krw"))
    items = []
    total = None
    offset = 0
    for _ in range(MAX_PAGES):
        data = api_get(session, {"count": "true", "q": action, "sr": f"|{sort}|{offset}|{PAGE_SIZE}"})
        results = data.get("SearchResults")
        if not isinstance(results, list):
            raise RuntimeError("Search API response is missing SearchResults")
        if total is None:
            try:
                total = int(data.get("Count", len(results)))
            except (TypeError, ValueError):
                total = len(results)
        items.extend(results)
        if not results or len(items) >= total or len(results) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    else:
        raise RuntimeError("Pagination safety limit reached")

    unique = {}
    for item in items:
        if item.get("Id") is not None:
            unique[str(item["Id"])] = item
    minimum = int(cfg.get("min_expected_results", 1))
    if len(unique) < minimum:
        raise RuntimeError("Search returned a suspiciously low result count; state preserved")
    if total and not unique:
        raise RuntimeError("Search reported results but no usable IDs were parsed")
    return list(unique.values())


def telegram_ready():
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(text):
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram HTTP {r.status_code}")


def safe_notify(text):
    try:
        if telegram_ready():
            send_telegram(text)
            return True
    except Exception as exc:
        log.error("Notification failed: %s", exc)
    return False


def value(car, *keys):
    for key in keys:
        if car.get(key) not in (None, ""):
            return car[key]
    return None


def format_car(label, car):
    car_id = str(car["Id"])
    title = " ".join(
        str(x)
        for x in [value(car, "Manufacturer"), value(car, "Model"), value(car, "Badge"), value(car, "BadgeDetail")]
        if x
    ) or label
    lines = ["🚨 YENİ ENCAR İLANI", f"Filtre: {label}", title]
    year = value(car, "FormYear", "Year")
    mileage = value(car, "Mileage")
    price = value(car, "Price")
    if year:
        lines.append(f"Yıl: {year}")
    if mileage is not None:
        try:
            lines.append(f"Km: {int(float(mileage)):,}".replace(",", "."))
        except (TypeError, ValueError):
            lines.append(f"Km: {mileage}")
    if price is not None:
        try:
            won = int(float(price)) * 10_000
            lines.append(f"Fiyat: {won:,} KRW".replace(",", "."))
        except (TypeError, ValueError):
            pass
    lines.extend([
        f"Encar ID: {car_id}",
        DETAIL_URL.format(car_id=car_id),
        f"İlk görülme: {datetime.now(BERLIN).strftime('%d.%m.%Y %H:%M')}",
    ])
    return "\n".join(lines)


def ensure_row(state, slot):
    return state["searches"].setdefault(slot, {
        "initialized": False,
        "seen": [],
        "is_failing": False,
        "last_error": None,
        "last_success": None,
        "last_result_count": None,
    })


def ping_healthcheck(success=True):
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return
    target = url.rstrip("/") if success else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=15).raise_for_status()
    except Exception:
        log.warning("Healthcheck ping failed")


def maybe_startup(state, config, counts, failed):
    if failed or state.get("startup_notified") or not telegram_ready():
        return
    if not all(
        ensure_row(state, state_slot(index))["initialized"]
        for index, _ in enumerate(config["searches"], start=1)
    ):
        return
    lines = ["✅ Cartrack aktif", "Mevcut ilanlar başlangıç listesine alındı; yeni ilan olarak bildirilmez."]
    for label, count in counts:
        lines.append(f"{label}: {count} mevcut ilan")
    lines.append("Tarama: her saat :07 ve :37")
    if safe_notify("\n".join(lines)):
        state["startup_notified"] = True


def maybe_daily(state, counts):
    if not telegram_ready():
        return
    now = datetime.now(BERLIN)
    today = now.date().isoformat()
    if now.hour < 19 or state.get("last_daily_summary_date") == today:
        return
    lines = ["💚 Cartrack günlük durum", "Sistem aktif."] + [f"{label}: {count} ilan" for label, count in counts]
    if safe_notify("\n".join(lines)):
        state["last_daily_summary_date"] = today


def run_watch(dry_run=False):
    config = load_config(required=True)
    require_env("STATE_KEY")
    state = load_state()
    session = requests.Session()
    bootstrap(session)
    any_failure = False
    counts = []

    for index, cfg in enumerate(config["searches"], start=1):
        slot = state_slot(index)
        row = ensure_row(state, slot)
        old_hashes = set(row.get("seen", []))
        try:
            listings = fetch_search(session, cfg)
            hashes = {digest_id(car["Id"]) for car in listings}
            counts.append((cfg["label"], len(listings)))
            log.info("Search %d OK: %d results", index, len(listings))
            if dry_run:
                continue
            if not row.get("initialized"):
                row["seen"] = sorted(hashes)
                row["initialized"] = True
                log.info("Search %d baseline created", index)
            else:
                fresh = [car for car in listings if digest_id(car["Id"]) not in old_hashes]
                log.info("Search %d new=%d", index, len(fresh))
                for car in reversed(fresh):
                    send_telegram(format_car(cfg["label"], car))
                    old_hashes.add(digest_id(car["Id"]))
                    row["seen"] = sorted(old_hashes)
                    time.sleep(0.4)
                row["seen"] = sorted(old_hashes | hashes)
            if row.get("is_failing"):
                safe_notify(f"✅ Cartrack düzeldi\n{cfg['label']} yeniden okunuyor.")
            row["is_failing"] = False
            row["last_error"] = None
            row["last_success"] = now_iso()
            row["last_result_count"] = len(listings)
        except Exception as exc:
            any_failure = True
            log.error("Search %d failed: %s", index, exc)
            if not dry_run:
                if not row.get("is_failing"):
                    safe_notify(f"⚠️ Cartrack hata\nFiltre: {cfg['label']}\nState korunuyor; tekrar denenecek.")
                row["is_failing"] = True
                row["last_error"] = type(exc).__name__

    if dry_run:
        if any_failure:
            raise RuntimeError("Dry-run failed")
        return

    maybe_startup(state, config, counts, any_failure)
    maybe_daily(state, counts)
    save_state(state)
    ping_healthcheck(success=not any_failure)
    if any_failure:
        raise RuntimeError("One or more searches failed")


def telegram_test():
    send_telegram("🧪 Cartrack test bildirimi başarılı. Telegram bağlantısı çalışıyor.")
    log.info("Telegram test sent")


def self_test():
    sample_id = "12345678"
    os.environ.setdefault("STATE_KEY", "self-test-only")
    a = digest_id(sample_id)
    b = digest_id(sample_id)
    c = digest_id("12345679")
    assert a == b and a != c and len(a) == 64
    assert state_slot(1) == "s1" and state_slot(5) == "s5"

    payload = {
        "type": "car",
        "action": "(And.Price.range(..1111).Test)",
        "sort": "MobileModifiedDate",
    }
    from urllib.parse import quote
    url = "https://car.encar.com/list/car?page=1&search=" + quote(json.dumps(payload, separators=(",", ":")))
    action, sort = decode_search_url(url, None)
    assert "Price.range(..1111)" in action
    assert legacy_price_cap(url) == 1211
    action, sort = decode_search_url(url, 2222)
    assert "Price.range(..2222)" in action and "Price.range(..1111)" not in action
    assert sort == "MobileModifiedDate"
    print("SELF_TEST_OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="watch", choices=["watch", "dry-run", "telegram-test", "self-test"])
    args = parser.parse_args()
    if args.mode == "watch":
        if not os.getenv("SEARCHES_JSON") or not os.getenv("STATE_KEY"):
            log.info("Runner is not configured yet; scheduled run skipped safely")
            return
        run_watch(False)
    elif args.mode == "dry-run":
        run_watch(True)
    elif args.mode == "telegram-test":
        telegram_test()
    else:
        self_test()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.error("Fatal: %s", exc)
        sys.exit(1)
