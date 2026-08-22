#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

API_URL = "https://api.encar.com/search/car/list/general"
DETAIL_URL = "https://fem.encar.com/cars/detail/{car_id}?listAdvType=share"
STATE_FILE = Path("state/state.json")
STATE_VERSION = 3
CONFIG_FINGERPRINT_VERSION = "cartrack-config-v3"
DEFAULT_TIMEZONE = "Europe/Berlin"
PAGE_SIZE = 100
MAX_PAGES = 20
RETRY_DELAYS_SECONDS = (5, 10)
LEGACY_PRICE_STEP_10K_KRW = 100
PRICE_UPPER_RE = re.compile(r"Price\.range\(\.\.(\d+)\)")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
API_HEADERS = {
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
log = logging.getLogger("cartrack")


@dataclass(frozen=True)
class SearchSpec:
    key: str
    label: str
    action: str
    sort: str
    min_expected_results: int


@dataclass(frozen=True)
class AppConfig:
    searches: tuple[SearchSpec, ...]
    timezone: ZoneInfo


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def decode_search_url(search_url: str) -> tuple[str, str]:
    parsed = urlparse(search_url)
    if parsed.scheme != "https" or parsed.netloc not in {"car.encar.com", "www.encar.com"}:
        raise ValueError("Unexpected Encar search URL host")

    values = parse_qs(parsed.query).get("search")
    if not values:
        raise ValueError("Encar search URL has no search parameter")

    try:
        payload = json.loads(values[0])
    except json.JSONDecodeError as exc:
        raise ValueError("Encar search parameter is not valid JSON") from exc

    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError("Encar search payload has no action")

    sort = payload.get("sort") or "MobileModifiedDate"
    if not isinstance(sort, str):
        raise ValueError("Encar search sort value is invalid")
    return action, sort


def migrate_v1_action(action: str) -> str:
    """Apply the one-time compatibility adjustment used by pre-v2 configurations."""

    def replace(match: re.Match[str]) -> str:
        cap = int(match.group(1)) + LEGACY_PRICE_STEP_10K_KRW
        return f"Price.range(..{cap})"

    return PRICE_UPPER_RE.sub(replace, action, count=1)


def load_config() -> tuple[AppConfig, str]:
    raw = require_env("SEARCHES_JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SEARCHES_JSON is invalid JSON") from exc

    if not isinstance(data, dict):
        raise RuntimeError("SEARCHES_JSON must be a JSON object")

    try:
        format_version = int(data.get("format_version", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("format_version must be an integer") from exc
    if format_version not in {1, 2}:
        raise RuntimeError("Unsupported format_version")

    timezone_name = data.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(timezone_name, str) or not timezone_name:
        raise RuntimeError("timezone must be a valid IANA timezone name")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("timezone must be a valid IANA timezone name") from exc

    rows = data.get("searches")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("SEARCHES_JSON must contain a non-empty searches list")

    seen_keys: set[str] = set()
    searches: list[SearchSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Every search config must be an object")

        key = row.get("key")
        label = row.get("label")
        search_url = row.get("search_url")
        if not isinstance(key, str) or not key:
            raise RuntimeError("Search config missing key")
        if not isinstance(label, str) or not label:
            raise RuntimeError("Search config missing label")
        if not isinstance(search_url, str) or not search_url:
            raise RuntimeError("Search config missing search_url")
        if key in seen_keys:
            raise RuntimeError("Duplicate search key")
        seen_keys.add(key)

        try:
            minimum = int(row.get("min_expected_results", 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("min_expected_results must be an integer") from exc
        if minimum < 1:
            raise RuntimeError("min_expected_results must be at least 1")

        action, sort = decode_search_url(search_url)
        if format_version == 1:
            action = migrate_v1_action(action)

        searches.append(
            SearchSpec(
                key=key,
                label=label,
                action=action,
                sort=sort,
                min_expected_results=minimum,
            )
        )

    return AppConfig(searches=tuple(searches), timezone=timezone), raw


def derive_state_key(raw_config: str) -> bytes:
    return hashlib.sha256(("cartrack-state-v2\0" + raw_config).encode("utf-8")).digest()


def config_fingerprint(raw_config: str) -> str:
    material = f"{CONFIG_FINGERPRINT_VERSION}\0{raw_config}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def digest_id(car_id: Any, state_key: bytes) -> str:
    return hmac.new(state_key, str(car_id).encode("utf-8"), hashlib.sha256).hexdigest()


def default_state(fingerprint: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "config_fingerprint": fingerprint,
        "searches": {},
        "startup_notified": False,
        "last_daily_summary_date": None,
    }


def load_state(fingerprint: str) -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state(fingerprint)

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("State file is unreadable; a fresh baseline will be created")
        return default_state(fingerprint)

    if (
        not isinstance(state, dict)
        or state.get("version") != STATE_VERSION
        or state.get("config_fingerprint") != fingerprint
        or not isinstance(state.get("searches"), dict)
    ):
        log.info("Configuration or state schema changed; a fresh baseline will be created")
        return default_state(fingerprint)

    state.setdefault("startup_notified", False)
    state.setdefault("last_daily_summary_date", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".json.tmp")
    temp_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_file.replace(STATE_FILE)


def state_slot(index: int) -> str:
    return f"s{index}"


def ensure_row(state: dict[str, Any], slot: str) -> dict[str, Any]:
    return state["searches"].setdefault(
        slot,
        {
            "initialized": False,
            "seen": [],
            "is_failing": False,
            "last_error": None,
            "last_success": None,
            "last_result_count": None,
        },
    )


def now_iso(timezone: ZoneInfo) -> str:
    return datetime.now(timezone).isoformat(timespec="seconds")


def bootstrap(session: requests.Session) -> None:
    for url in ("https://www.encar.com/", "https://car.encar.com/"):
        try:
            response = session.get(url, headers=PAGE_HEADERS, timeout=20, allow_redirects=True)
            if response.status_code == 200:
                log.info("Session bootstrap OK")
                return
        except requests.RequestException:
            continue
    log.warning("Session bootstrap did not return HTTP 200; API retries will decide final status")


def api_get(session: requests.Session, params: dict[str, str]) -> dict[str, Any]:
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    last_error: Exception | None = None

    for attempt in range(attempts):
        try:
            response = session.get(API_URL, params=params, headers=API_HEADERS, timeout=25)
            if response.status_code != 200:
                raise RuntimeError(f"Search API HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Search API returned invalid JSON")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc

        if attempt < len(RETRY_DELAYS_SECONDS):
            log.warning("Search request failed; retry %d/%d", attempt + 1, attempts)
            time.sleep(RETRY_DELAYS_SECONDS[attempt])

    raise RuntimeError(f"Search request failed after {attempts} attempts: {last_error}")


def fetch_search(session: requests.Session, spec: SearchSpec) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    total: int | None = None
    offset = 0

    for _ in range(MAX_PAGES):
        data = api_get(
            session,
            {
                "count": "true",
                "q": spec.action,
                "sr": f"|{spec.sort}|{offset}|{PAGE_SIZE}",
            },
        )
        results = data.get("SearchResults")
        if not isinstance(results, list):
            raise RuntimeError("Search API response is missing SearchResults")

        if total is None:
            try:
                total = int(data.get("Count", len(results)))
            except (TypeError, ValueError):
                total = len(results)

        items.extend(item for item in results if isinstance(item, dict))
        if not results or (total is not None and len(items) >= total) or len(results) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    else:
        raise RuntimeError("Pagination safety limit reached")

    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        car_id = item.get("Id")
        if car_id is not None:
            unique[str(car_id)] = item

    if len(unique) < spec.min_expected_results:
        raise RuntimeError("Search returned a suspiciously low result count; state preserved")
    if total and not unique:
        raise RuntimeError("Search reported results but no usable IDs were parsed")
    return list(unique.values())


def telegram_ready() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram(text: str) -> None:
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("Telegram API did not confirm message delivery")


def safe_notify(text: str) -> bool:
    if not telegram_ready():
        return False
    try:
        send_telegram(text)
        return True
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        log.error("Notification failed: %s", type(exc).__name__)
        return False


def first_value(car: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = car.get(key)
        if value not in (None, ""):
            return value
    return None


def format_car(label: str, car: dict[str, Any], timezone: ZoneInfo) -> str:
    car_id = str(car["Id"])
    title = " ".join(
        str(value)
        for value in (
            first_value(car, "Manufacturer"),
            first_value(car, "Model"),
            first_value(car, "Badge"),
            first_value(car, "BadgeDetail"),
        )
        if value
    ) or label

    lines = ["🚨 YENİ ENCAR İLANI", f"Filtre: {label}", title]
    year = first_value(car, "FormYear", "Year")
    mileage = first_value(car, "Mileage")
    price = first_value(car, "Price")

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

    lines.extend(
        [
            f"Encar ID: {car_id}",
            DETAIL_URL.format(car_id=car_id),
            f"İlk görülme: {datetime.now(timezone).strftime('%d.%m.%Y %H:%M')}",
        ]
    )
    return "\n".join(lines)


def ping_healthcheck(success: bool) -> None:
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return
    target = url.rstrip("/") if success else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=15).raise_for_status()
    except requests.RequestException:
        log.warning("Healthcheck ping failed")


def maybe_startup(
    state: dict[str, Any],
    config: AppConfig,
    counts: list[tuple[str, int]],
    run_failed: bool,
) -> None:
    if run_failed or state.get("startup_notified") or not telegram_ready():
        return
    if not all(
        ensure_row(state, state_slot(index))["initialized"]
        for index, _ in enumerate(config.searches, start=1)
    ):
        return

    lines = [
        "✅ Cartrack aktif",
        "Mevcut ilanlar başlangıç listesine alındı; yeni ilan olarak bildirilmez.",
    ]
    lines.extend(f"{label}: {count} mevcut ilan" for label, count in counts)
    lines.append("Tarama: her saat :07 ve :37")
    if safe_notify("\n".join(lines)):
        state["startup_notified"] = True


def maybe_daily(state: dict[str, Any], counts: list[tuple[str, int]], timezone: ZoneInfo) -> None:
    if not telegram_ready():
        return

    now = datetime.now(timezone)
    today = now.date().isoformat()
    if now.hour < 19 or state.get("last_daily_summary_date") == today:
        return

    lines = ["💚 Cartrack günlük durum", "Sistem aktif."]
    lines.extend(f"{label}: {count} ilan" for label, count in counts)
    if safe_notify("\n".join(lines)):
        state["last_daily_summary_date"] = today


def process_search(
    session: requests.Session,
    state: dict[str, Any],
    state_key: bytes,
    spec: SearchSpec,
    index: int,
    timezone: ZoneInfo,
    dry_run: bool,
) -> tuple[int | None, bool]:
    slot = state_slot(index)
    row = ensure_row(state, slot)

    try:
        listings = fetch_search(session, spec)
    except Exception as exc:
        log.error("Search %d failed: %s", index, type(exc).__name__)
        if not dry_run:
            if not row.get("is_failing"):
                safe_notify(
                    f"⚠️ Cartrack hata\nFiltre: {spec.label}\nState korunuyor; tekrar denenecek."
                )
            row["is_failing"] = True
            row["last_error"] = type(exc).__name__
        return None, True

    count = len(listings)
    log.info("Search %d OK: %d results", index, count)
    if dry_run:
        return count, False

    was_failing = bool(row.get("is_failing"))
    row["is_failing"] = False
    row["last_error"] = None
    row["last_success"] = now_iso(timezone)
    row["last_result_count"] = count

    current_hashes = {digest_id(car["Id"], state_key) for car in listings}
    old_hashes = set(row.get("seen", []))

    if not row.get("initialized"):
        row["seen"] = sorted(current_hashes)
        row["initialized"] = True
        log.info("Search %d baseline created", index)
    else:
        fresh = [car for car in listings if digest_id(car["Id"], state_key) not in old_hashes]
        log.info("Search %d new=%d", index, len(fresh))
        delivered_hashes = set(old_hashes)

        for car in reversed(fresh):
            car_hash = digest_id(car["Id"], state_key)
            try:
                send_telegram(format_car(spec.label, car, timezone))
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                log.error("Telegram delivery failed for search %d: %s", index, type(exc).__name__)
                row["seen"] = sorted(delivered_hashes)
                return count, True
            delivered_hashes.add(car_hash)
            row["seen"] = sorted(delivered_hashes)
            time.sleep(0.4)

        row["seen"] = sorted(delivered_hashes | current_hashes)

    if was_failing:
        safe_notify(f"✅ Cartrack düzeldi\n{spec.label} yeniden okunuyor.")
    return count, False


def run_watch(dry_run: bool = False) -> None:
    config, raw_config = load_config()
    fingerprint = config_fingerprint(raw_config)
    state_key = derive_state_key(raw_config)
    state = load_state(fingerprint)

    session = requests.Session()
    bootstrap(session)

    run_failed = False
    counts: list[tuple[str, int]] = []
    for index, spec in enumerate(config.searches, start=1):
        count, failed = process_search(
            session=session,
            state=state,
            state_key=state_key,
            spec=spec,
            index=index,
            timezone=config.timezone,
            dry_run=dry_run,
        )
        run_failed = run_failed or failed
        if count is not None:
            counts.append((spec.label, count))

    if dry_run:
        if run_failed:
            raise RuntimeError("Dry-run failed")
        return

    maybe_startup(state, config, counts, run_failed)
    if not run_failed:
        maybe_daily(state, counts, config.timezone)
    save_state(state)
    ping_healthcheck(success=not run_failed)

    if run_failed:
        raise RuntimeError("One or more checks failed")


def telegram_test() -> None:
    send_telegram("🧪 Cartrack test bildirimi başarılı. Telegram bağlantısı çalışıyor.")
    log.info("Telegram test sent")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Encar searches and send Telegram alerts.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="watch",
        choices=("watch", "dry-run", "telegram-test"),
    )
    args = parser.parse_args()

    if args.mode == "telegram-test":
        telegram_test()
        return

    if args.mode == "watch" and not os.getenv("SEARCHES_JSON"):
        log.info("Runner is not configured yet; scheduled run skipped safely")
        return

    run_watch(dry_run=args.mode == "dry-run")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.error("Fatal: %s", type(exc).__name__)
        sys.exit(1)
