#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

API_URL = "https://api.encar.com/search/car/list/general"
VEHICLE_DETAIL_API = (
    "https://api.encar.com/v1/readside/vehicle/{car_id}"
    "?include=MANAGE,SPEC,CONDITION,ADVERTISEMENT"
)
DETAIL_URL = "https://fem.encar.com/cars/detail/{car_id}?listAdvType=share"
STATE_FILE = Path(os.getenv("CARTRACK_STATE_FILE", ".state/state.enc"))
STATE_VERSION = 4
CONFIG_FINGERPRINT_VERSION = "cartrack-config-v4"
DEFAULT_TIMEZONE = "Europe/Berlin"
ENCAR_TIMEZONE = ZoneInfo("Asia/Seoul")
PAGE_SIZE = 100
MAX_PAGES = 20
RETRY_DELAYS_SECONDS = (5, 10)
DETAIL_RETRY_DELAYS_SECONDS = (2, 5)
TELEGRAM_MIN_INTERVAL_SECONDS = 1.1
TELEGRAM_MAX_ATTEMPTS = 4
LEGACY_PRICE_STEP_10K_KRW = 100
PRICE_UPPER_RE = re.compile(r"Price\.range\(\.\.(\d+)\)")
MILEAGE_UPPER_RE = re.compile(r"Mileage\.range\(\.\.\d+\)")
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


@dataclass(frozen=True)
class ListingContext:
    re_registered: bool | None = None
    registered_at: datetime | None = None
    advertised_at: datetime | None = None
    modified_at: datetime | None = None


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
    """Compatibility adjustment for installations created before format_version 2."""

    def replace(match: re.Match[str]) -> str:
        return f"Price.range(..{int(match.group(1)) + LEGACY_PRICE_STEP_10K_KRW})"

    return PRICE_UPPER_RE.sub(replace, action, count=1)


def load_config() -> AppConfig:
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
            minimum = int(row.get("min_expected_results", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("min_expected_results must be an integer") from exc
        if minimum < 0:
            raise RuntimeError("min_expected_results must be zero or greater")

        action, sort = decode_search_url(search_url)
        if format_version == 1:
            action = migrate_v1_action(action)

        forced_mileage = os.getenv("CARTRACK_MAX_MILEAGE_KM", "130000")
        if forced_mileage:
            try:
                forced_mileage_int = int(forced_mileage)
            except ValueError as exc:
                raise RuntimeError("CARTRACK_MAX_MILEAGE_KM must be an integer") from exc
            if forced_mileage_int <= 0:
                raise RuntimeError("CARTRACK_MAX_MILEAGE_KM must be positive")
            action = MILEAGE_UPPER_RE.sub(f"Mileage.range(..{forced_mileage_int})", action, count=1)

        searches.append(
            SearchSpec(
                key=key,
                label=label,
                action=action,
                sort=sort,
                min_expected_results=minimum,
            )
        )

    return AppConfig(searches=tuple(searches), timezone=timezone)


def canonical_config(config: AppConfig) -> bytes:
    rows = [
        {"key": spec.key, "action": spec.action, "sort": spec.sort}
        for spec in sorted(config.searches, key=lambda item: item.key)
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def config_fingerprint(config: AppConfig) -> str:
    material = CONFIG_FINGERPRINT_VERSION.encode("utf-8") + b"\0" + canonical_config(config)
    return hashlib.sha256(material).hexdigest()[:24]


def state_secret() -> str:
    explicit = os.getenv("STATE_ENCRYPTION_KEY")
    if explicit:
        return explicit
    return require_env("TELEGRAM_BOT_TOKEN")


def derive_key(secret: str, purpose: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


def digest_id(car_id: Any, hmac_key: bytes) -> str:
    return hmac.new(hmac_key, str(car_id).encode("utf-8"), hashlib.sha256).hexdigest()


def default_state(fingerprint: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "config_fingerprint": fingerprint,
        "searches": {},
        "startup_notified": False,
        "last_daily_summary_date": None,
    }


def encrypt_state(state: dict[str, Any], secret: str) -> bytes:
    key = derive_key(secret, "cartrack-state-aesgcm-v1")
    nonce = os.urandom(12)
    plaintext = json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, b"cartrack-state-v4")
    envelope = {
        "v": 1,
        "n": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "c": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decrypt_state(payload: bytes, secret: str) -> dict[str, Any]:
    try:
        envelope = json.loads(payload.decode("utf-8"))
        nonce = base64.urlsafe_b64decode(envelope["n"])
        ciphertext = base64.urlsafe_b64decode(envelope["c"])
        key = derive_key(secret, "cartrack-state-aesgcm-v1")
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, b"cartrack-state-v4")
        state = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Encrypted state cannot be decoded") from exc
    if not isinstance(state, dict):
        raise RuntimeError("Encrypted state has invalid content")
    return state


def load_state(fingerprint: str, secret: str) -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state(fingerprint)

    try:
        payload = STATE_FILE.read_bytes()
    except OSError as exc:
        raise RuntimeError("Runtime state cannot be read; refusing to reset baseline") from exc

    state = decrypt_state(payload, secret)

    if state.get("version") != STATE_VERSION:
        raise RuntimeError("Unsupported runtime state version; refusing to reset baseline")
    if not isinstance(state.get("searches"), dict):
        raise RuntimeError("Runtime state is invalid; refusing to reset baseline")

    if state.get("config_fingerprint") != fingerprint:
        log.info("Search configuration changed; preserving history and refreshing baseline")
        state["config_fingerprint"] = fingerprint
        for row in state["searches"].values():
            if isinstance(row, dict):
                row["initialized"] = False
        return state

    state.setdefault("startup_notified", False)
    state.setdefault("last_daily_summary_date", None)
    return state


def save_state(state: dict[str, Any], secret: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".enc.tmp")
    temp_file.write_bytes(encrypt_state(state, secret))
    temp_file.replace(STATE_FILE)


def ensure_row(state: dict[str, Any], key: str) -> dict[str, Any]:
    return state["searches"].setdefault(
        key,
        {
            "initialized": False,
            "seen": [],
            "fetch_failing": False,
            "delivery_failing": False,
        },
    )


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

    if total and not unique:
        raise RuntimeError("Search reported results but no usable IDs were parsed")
    if len(unique) < spec.min_expected_results:
        raise RuntimeError("Search returned fewer results than configured minimum; state preserved")
    return list(unique.values())


def parse_encar_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ENCAR_TIMEZONE)
    return parsed


def fetch_listing_context(session: requests.Session, car_id: Any) -> ListingContext | None:
    url = VEHICLE_DETAIL_API.format(car_id=car_id)
    attempts = len(DETAIL_RETRY_DELAYS_SECONDS) + 1

    for attempt in range(attempts):
        try:
            response = session.get(url, headers=API_HEADERS, timeout=20)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("Vehicle detail API returned invalid JSON")
            manage = data.get("manage") if isinstance(data.get("manage"), dict) else {}
            return ListingContext(
                re_registered=manage.get("reRegistered") if isinstance(manage.get("reRegistered"), bool) else None,
                registered_at=parse_encar_datetime(manage.get("registDateTime")),
                advertised_at=parse_encar_datetime(manage.get("firstAdvertisedDateTime")),
                modified_at=parse_encar_datetime(manage.get("modifyDateTime")),
            )
        except (requests.RequestException, ValueError, RuntimeError):
            if attempt < len(DETAIL_RETRY_DELAYS_SECONDS):
                time.sleep(DETAIL_RETRY_DELAYS_SECONDS[attempt])

    log.warning("Listing detail lookup failed for a newly matched listing")
    return None


def classify_listing(context: ListingContext | None, now: datetime) -> str:
    if context is None:
        return "matched"
    if context.re_registered is True:
        return "reregistered"
    if context.advertised_at is not None:
        advertised = context.advertised_at.astimezone(now.tzinfo) if now.tzinfo else context.advertised_at
        if now - timedelta(hours=24) <= advertised <= now + timedelta(minutes=10):
            return "new"
        return "matched"
    return "matched"


def telegram_ready() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def telegram_retry_after(response: requests.Response) -> float | None:
    try:
        payload = response.json()
        value = payload.get("parameters", {}).get("retry_after")
        return float(value) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def send_telegram(text: str) -> None:
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    last_error: Exception | None = None

    for attempt in range(TELEGRAM_MAX_ATTEMPTS):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
                timeout=20,
            )
            if response.status_code == 429:
                delay = telegram_retry_after(response) or min(2**attempt, 8)
                last_error = RuntimeError("Telegram rate limited")
            elif response.status_code >= 500:
                delay = min(2**attempt, 8)
                last_error = RuntimeError(f"Telegram HTTP {response.status_code}")
            else:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("ok") is not True:
                    raise RuntimeError("Telegram API did not confirm message delivery")
                return
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            delay = min(2**attempt, 8)

        if attempt < TELEGRAM_MAX_ATTEMPTS - 1:
            time.sleep(delay)

    raise RuntimeError("Telegram delivery failed") from last_error


def safe_notify(text: str) -> bool:
    if not telegram_ready():
        return False
    try:
        send_telegram(text)
        return True
    except RuntimeError:
        log.error("Notification failed")
        return False


def first_value(car: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = car.get(key)
        if value not in (None, ""):
            return value
    return None


def format_local_datetime(value: datetime | None, timezone: ZoneInfo) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone).strftime("%d.%m.%Y %H:%M")


def format_car(
    label: str,
    car: dict[str, Any],
    timezone: ZoneInfo,
    context: ListingContext | None = None,
) -> str:
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

    now = datetime.now(timezone)
    classification = classify_listing(context, now)
    if classification == "reregistered":
        headline = "♻️ ENCAR'DA YENİDEN İLANA ALINDI"
    elif classification == "new":
        headline = "🚨 YENİ ENCAR İLANI"
    else:
        headline = "🔔 FİLTREYE YENİ GİREN ENCAR İLANI"

    lines = [headline, f"Filtre: {label}", title]
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

    if context is not None:
        advertised = format_local_datetime(context.advertised_at, timezone)
        registered = format_local_datetime(context.registered_at, timezone)
        if advertised:
            lines.append(f"Encar yayın zamanı: {advertised}")
        if context.re_registered is True and registered:
            lines.append(f"İlk kayıt: {registered}")

    lines.extend(
        [
            f"Encar ID: {car_id}",
            DETAIL_URL.format(car_id=car_id),
            f"Sistemde ilk görülme: {now.strftime('%d.%m.%Y %H:%M')}",
        ]
    )
    return "\n".join(lines)


def ping_healthcheck(success: bool) -> None:
    success_url = os.getenv("HEALTHCHECK_SUCCESS_URL") or os.getenv("HEALTHCHECK_URL")
    failure_url = os.getenv("HEALTHCHECK_FAILURE_URL")
    if not failure_url and success_url:
        failure_url = success_url.rstrip("/") + "/fail"
    target = success_url if success else failure_url
    if not target:
        return
    try:
        requests.get(target, timeout=15).raise_for_status()
    except requests.RequestException:
        log.warning("Healthcheck ping failed")


def maybe_startup(
    state: dict[str, Any],
    config: AppConfig,
    counts: list[tuple[str, int]],
    run_failed: bool,
) -> bool:
    if run_failed or state.get("startup_notified") or not telegram_ready():
        return False
    if not all(ensure_row(state, spec.key)["initialized"] for spec in config.searches):
        return False

    lines = [
        "✅ Cartrack aktif",
        "Mevcut ilanlar başlangıç listesine alındı; yeni ilan olarak bildirilmez.",
    ]
    lines.extend(f"{label}: {count} mevcut ilan" for label, count in counts)
    lines.append("Tarama: her saat :07 ve :37")
    if safe_notify("\n".join(lines)):
        state["startup_notified"] = True
        return True
    return False


def maybe_daily(state: dict[str, Any], counts: list[tuple[str, int]], timezone: ZoneInfo) -> bool:
    if not telegram_ready():
        return False

    now = datetime.now(timezone)
    today = now.date().isoformat()
    if now.hour < 19 or state.get("last_daily_summary_date") == today:
        return False

    lines = ["💚 Cartrack günlük durum", "Sistem aktif."]
    lines.extend(f"{label}: {count} ilan" for label, count in counts)
    if safe_notify("\n".join(lines)):
        state["last_daily_summary_date"] = today
        return True
    return False


def process_search(
    session: requests.Session,
    state: dict[str, Any],
    hmac_key: bytes,
    spec: SearchSpec,
    index: int,
    timezone: ZoneInfo,
    dry_run: bool,
) -> tuple[int | None, bool, bool]:
    row = ensure_row(state, spec.key)

    try:
        listings = fetch_search(session, spec)
    except Exception as exc:
        log.error("Search %d failed: %s", index, type(exc).__name__)
        changed = False
        if not dry_run and not row.get("fetch_failing"):
            safe_notify(f"⚠️ Cartrack hata\nFiltre: {spec.label}\nState korunuyor; tekrar denenecek.")
            row["fetch_failing"] = True
            changed = True
        return None, True, changed

    count = len(listings)
    log.info("Search %d OK: %d results", index, count)
    if dry_run:
        return count, False, False

    changed = False
    if row.get("fetch_failing"):
        row["fetch_failing"] = False
        changed = True
        safe_notify(f"✅ Cartrack düzeldi\n{spec.label} yeniden okunuyor.")

    current_hashes = {digest_id(car["Id"], hmac_key) for car in listings}
    old_hashes = set(row.get("seen", []))

    if not row.get("initialized"):
        row["seen"] = sorted(old_hashes | current_hashes)
        row["initialized"] = True
        row["delivery_failing"] = False
        changed = True
        log.info("Search %d baseline created", index)
        return count, False, changed

    fresh = [car for car in listings if digest_id(car["Id"], hmac_key) not in old_hashes]
    log.info("Search %d new=%d", index, len(fresh))
    delivered_hashes = set(old_hashes)

    for car in reversed(fresh):
        car_hash = digest_id(car["Id"], hmac_key)
        context = fetch_listing_context(session, car["Id"])
        try:
            send_telegram(format_car(spec.label, car, timezone, context))
        except RuntimeError:
            log.error("Telegram delivery failed for search %d", index)
            if not row.get("delivery_failing"):
                row["delivery_failing"] = True
                changed = True
            row["seen"] = sorted(delivered_hashes)
            return count, True, changed
        delivered_hashes.add(car_hash)
        row["seen"] = sorted(delivered_hashes)
        changed = True
        time.sleep(TELEGRAM_MIN_INTERVAL_SECONDS)

    if row.get("delivery_failing"):
        row["delivery_failing"] = False
        changed = True

    merged = delivered_hashes | current_hashes
    if merged != old_hashes:
        row["seen"] = sorted(merged)
        changed = True
    return count, False, changed


def run_watch(dry_run: bool = False) -> None:
    config = load_config()
    fingerprint = config_fingerprint(config)
    secret = state_secret()
    hmac_key = derive_key(secret, "cartrack-listing-hmac-v1")
    state = load_state(fingerprint, secret)

    session = requests.Session()
    bootstrap(session)

    run_failed = False
    state_changed = False
    counts: list[tuple[str, int]] = []
    for index, spec in enumerate(config.searches, start=1):
        count, failed, changed = process_search(
            session=session,
            state=state,
            hmac_key=hmac_key,
            spec=spec,
            index=index,
            timezone=config.timezone,
            dry_run=dry_run,
        )
        run_failed = run_failed or failed
        state_changed = state_changed or changed
        if count is not None:
            counts.append((spec.label, count))

    if dry_run:
        if run_failed:
            raise RuntimeError("Dry-run failed")
        return

    state_changed = maybe_startup(state, config, counts, run_failed) or state_changed
    if not run_failed:
        state_changed = maybe_daily(state, counts, config.timezone) or state_changed

    if state_changed or not STATE_FILE.exists():
        save_state(state, secret)
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
