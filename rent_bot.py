"""Standalone Telegram bot for OLX, Otodom and Nieruchomości-online.

Install:
    python3 -m pip install requests beautifulsoup4 pyTelegramBotAPI
Optional browser fallback:
    python3 -m pip install playwright && playwright install chromium
Run:
    export TELEGRAM_BOT_TOKEN='token_from_BotFather'
    python3 rent_bot_one_file.py
"""

from __future__ import annotations

import html as html_lib
import io
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
import telebot
from bs4 import BeautifulSoup, Tag
from telebot import types


# =============================================================================
# Configuration
# =============================================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN before starting the bot.")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("RENT_BOT_DB", str(BASE_DIR / "rent_bot.db")))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
SUPPORTED_DOMAINS = ("olx.pl", "otodom.pl", "nieruchomosci-online.pl")
MONEY_RE = re.compile(
    r"(?<![\d.,])(?P<amount>\d{1,3}(?:[\s\u00a0.,]\d{3})*|\d+)(?:[,.]\d{1,2})?\s*"
    r"(?P<currency>zł|zl|pln)\b",
    re.IGNORECASE,
)
FEE_RE = re.compile(
    r"\b(?:czynsz\s+(?:administracyjny|do\s+wspólnoty)|"
    r"opłat[ay]\s+(?:administracyjn[ey]|do\s+wspólnoty|eksploatacyjn[ey])|"
    r"opłata\s+administracyjna|wspólnot[ay])\b",
    re.IGNORECASE,
)
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")


class ListingError(RuntimeError):
    """A safe error message to display in Telegram."""


@dataclass
class Listing:
    title: str
    price: Optional[str]
    czynsz: Optional[str]
    description: Optional[str]
    photos: list[str] = field(default_factory=list)
    url: str = ""

    def usable(self) -> bool:
        return bool(self.title and self.price)


# =============================================================================
# Listing parser
# =============================================================================

def is_supported_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except (TypeError, ValueError):
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in SUPPORTED_DOMAINS)


def clean_url(url: str) -> str:
    if not is_supported_url(url):
        raise ListingError("Поддерживаются только ссылки OLX, Otodom и Nieruchomości-online.")
    parsed = urlparse(url.strip())
    # Query parameters are not needed for a direct listing and often contain trackers.
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def normal_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def multiline_text(value: str) -> str:
    return "\n".join(
        line for line in (normal_text(line) for line in value.replace("\r", "\n").split("\n")) if line
    )


def html_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return multiline_text(BeautifulSoup(value, "html.parser").get_text("\n", strip=True))


def format_money(match: re.Match[str]) -> str:
    return f"{normal_text(match.group('amount'))} zł"


def first_money(text: str, skip_per_m2: bool = True) -> Optional[str]:
    text = normal_text(text)
    for match in MONEY_RE.finditer(text):
        tail = text[match.end() : match.end() + 16].lower()
        if skip_per_m2 and re.search(r"(?:/|za)?\s*m(?:²|2)\b", tail):
            continue
        return format_money(match)
    return None


def money_number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = MONEY_RE.search(normal_text(value))
    if not match:
        return None
    raw = re.sub(r"\s+", "", match.group("amount"))
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = raw.replace(".", "").replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def same_money(first: Optional[str], second: Optional[str]) -> bool:
    a, b = money_number(first), money_number(second)
    return a is not None and b is not None and abs(a - b) < 0.005


def decode_json(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    # Accept common JavaScript hydration assignments such as window.__DATA__ = {...}.
    for start in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text[start.start() :])
            return value
        except json.JSONDecodeError:
            continue
    return None


def script_payloads(soup: BeautifulSoup) -> list[Any]:
    payloads: list[Any] = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if script_type not in {"application/ld+json", "application/json"} and script.get("id") != "__NEXT_DATA__":
            continue
        data = decode_json(script.get_text("", strip=False))
        if data is not None:
            payloads.append(data)
    return payloads


def walk_json(value: Any, max_nodes: int = 10_000) -> Iterator[Any]:
    stack: list[tuple[Any, int]] = [(value, 0)]
    count = 0
    while stack and count < max_nodes:
        node, depth = stack.pop()
        count += 1
        yield node
        if depth >= 24:
            continue
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def get_key(data: dict[str, Any], *keys: str) -> Any:
    wanted = {key.lower() for key in keys}
    for key, value in data.items():
        if str(key).lower() in wanted and value not in (None, "", [], {}):
            return value
    return None


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return html_text(value) if "<" in value and ">" in value else multiline_text(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return as_text(get_key(value, "label", "displayValue", "value", "amount", "text", "name"))
    return ""


def meta_content(soup: BeautifulSoup, *names: str) -> Optional[str]:
    expected = {name.lower() for name in names}
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in expected and meta.get("content"):
            return normal_text(meta["content"])
    return None


def element_text(tag: Tag) -> str:
    copy = BeautifulSoup(str(tag), "html.parser")
    for unnecessary in copy.select("script, style, button, svg, noscript, [aria-hidden='true']"):
        unnecessary.decompose()
    return multiline_text(copy.get_text("\n", strip=True))


def price_from_value(value: Any) -> Optional[str]:
    if isinstance(value, (int, float)):
        return f"{value:g} zł"
    if isinstance(value, str):
        return first_money(value) or (f"{normal_text(value)} zł" if re.fullmatch(r"\d+(?:[,.]\d+)?", value.strip()) else None)
    if isinstance(value, dict):
        display = as_text(get_key(value, "displayValue", "label", "formatted", "value"))
        if first_money(display):
            return first_money(display)
        amount = get_key(value, "amount", "value", "price")
        currency = as_text(get_key(value, "currency", "currencyCode")) or "PLN"
        if currency.lower() in {"pln", "zł", "zl"}:
            return price_from_value(amount)
    return None


def extract_title(soup: BeautifulSoup, payloads: list[Any]) -> Optional[str]:
    # h1 is safer than a site-wide title inside a huge hydration payload.
    heading = soup.select_one("[data-cy='ad_title'] h1, [data-testid*='title'] h1, h1")
    if heading:
        text = element_text(heading)
        if text:
            return text
    for payload in payloads:
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            title = as_text(get_key(node, "title", "headline", "name"))
            keys = {str(key).lower() for key in node}
            if title and len(title) <= 300 and (
                keys & {"title", "headline", "description", "images", "photos", "price", "target"}
            ):
                return title
    return meta_content(soup, "og:title", "twitter:title")


def extract_price(soup: BeautifulSoup, payloads: list[Any]) -> Optional[str]:
    candidates: list[tuple[int, str]] = []
    for payload in payloads:
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            for key in ("price", "totalPrice", "offerPrice", "regularPrice"):
                value = get_key(node, key)
                price = price_from_value(value)
                if price:
                    candidates.append((100 if key.lower() == "price" else 90, price))
            offer = get_key(node, "offers", "offer")
            if isinstance(offer, dict):
                price = price_from_value(get_key(offer, "price", "priceSpecification"))
                if price:
                    candidates.append((95, price))

    selectors = (
        "[itemprop='price']", "[data-cy='ad-price-container']", "[data-testid*='price']",
        "[class*='offer-price']", "[class*='property-price']", "[class*='price']",
        "[class*='cena']", "[class*='koszt']",
    )
    for selector in selectors:
        for tag in soup.select(selector):
            text = element_text(tag)
            price = first_money(text)
            if not price:
                continue
            lowered = normal_text(text).lower()
            score = 85
            if "kaucj" in lowered or "prowizj" in lowered:
                score -= 70
            if "czynsz administracyjny" in lowered or "opłat" in lowered:
                score -= 55
            if tag.get("itemprop") == "price":
                score += 15
            candidates.append((score, price))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def extract_description(soup: BeautifulSoup, payloads: list[Any]) -> Optional[str]:
    candidates: list[tuple[int, str]] = []
    for payload in payloads:
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            text = as_text(get_key(node, "description", "descriptionHtml", "descriptionText", "body"))
            if len(text) >= 30:
                candidates.append((85, text))

    for selector in (
        "[data-cy='ad_description']", "[data-testid*='description']", "#description",
        ".offer-description", ".offer__description", ".description__content", ".description-content",
    ):
        for tag in soup.select(selector):
            text = element_text(tag)
            if len(text) >= 30:
                candidates.append((100, text))

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        if not re.search(r"\b(opis|description)\b", normal_text(heading.get_text(" ", strip=True)).lower()):
            continue
        content = heading.find_next_sibling()
        if isinstance(content, Tag):
            text = element_text(content)
            if len(text) >= 30:
                candidates.append((95, text))

    description_meta = meta_content(soup, "description", "og:description")
    if description_meta and len(description_meta) >= 30:
        candidates.append((20, description_meta))
    if not candidates:
        return None
    # Within trusted sources, prefer the full text over a collapsed preview.
    candidates.sort(key=lambda item: (item[0], min(len(item[1]), 12_000)), reverse=True)
    return candidates[0][1][:12_000]


def money_after_label(text: str, label: re.Pattern[str]) -> list[str]:
    result: list[str] = []
    text = normal_text(text)
    for match in label.finditer(text):
        after = first_money(text[match.end() : match.end() + 100])
        if after:
            result.append(after)
    return result


def extract_czynsz(soup: BeautifulSoup, description: Optional[str], price: Optional[str]) -> Optional[str]:
    blocks = [description or ""]
    for tag in soup.find_all(["li", "tr", "dt", "dd", "p", "div", "span"]):
        text = normal_text(tag.get_text(" ", strip=True))
        if FEE_RE.search(text):
            blocks.append(text)
    for term in soup.find_all("dt"):
        label = normal_text(term.get_text(" ", strip=True))
        value = term.find_next_sibling("dd")
        if value and FEE_RE.search(label):
            blocks.append(f"{label}: {normal_text(value.get_text(' ', strip=True))}")

    for block in blocks:
        for fee in money_after_label(block, FEE_RE):
            if not same_money(fee, price):
                return fee

    # Plain "czynsz" is accepted only when it is not the rent itself.
    for block in blocks:
        for match in re.finditer(r"\bczynsz\b", normal_text(block), re.IGNORECASE):
            context = normal_text(block)[match.start() : match.start() + 32].lower()
            if re.search(r"czynsz\s+(?:najmu|miesięczny|za\s+najem)", context):
                continue
            fee = first_money(normal_text(block)[match.end() : match.end() + 90])
            if fee and not same_money(fee, price):
                return fee
    return None


def best_srcset(srcset: str) -> Optional[str]:
    options: list[tuple[int, str]] = []
    for part in srcset.split(","):
        pieces = part.strip().split()
        if not pieces:
            continue
        width_match = re.match(r"(\d+)w", pieces[-1]) if len(pieces) > 1 else None
        options.append((int(width_match.group(1)) if width_match else 0, pieces[0]))
    return max(options, default=(0, ""))[1] or None


def image_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from image_values(child)
    elif isinstance(value, dict):
        direct = get_key(value, "url", "src", "link", "contentUrl", "large", "medium", "original")
        if direct is not None:
            yield from image_values(direct)
        else:
            for child in value.values():
                yield from image_values(child)


def is_listing_image(url: str) -> bool:
    low = url.lower()
    return low.startswith(("https://", "http://")) and not any(
        word in low for word in ("logo", "favicon", "avatar", "profile", "placeholder", "sprite", "icon")
    )


def extract_photos(soup: BeautifulSoup, payloads: list[Any], base_url: str) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    ordinal = 0

    def add(raw_url: Any, score: int) -> None:
        nonlocal ordinal
        if not isinstance(raw_url, str):
            return
        url, _ = urldefrag(urljoin(base_url, raw_url.strip()))
        if is_listing_image(url):
            candidates.append((score, ordinal, url))
            ordinal += 1

    for payload in payloads:
        for node in walk_json(payload):
            if isinstance(node, dict):
                for key, value in node.items():
                    if str(key).lower() in {"image", "images", "photo", "photos", "gallery"}:
                        for image_url in image_values(value):
                            add(image_url, 100)
    for value in (meta_content(soup, "og:image"), meta_content(soup, "twitter:image")):
        if value:
            add(value, 70)

    gallery_selectors = (
        "[data-cy*='image'] img", "[data-cy*='gallery'] img", "[data-testid*='gallery'] img",
        "[data-testid*='image'] img", "[class*='gallery'] img", "[class*='slider'] img", "[class*='carousel'] img",
    )
    gallery_ids: set[int] = set()
    for selector in gallery_selectors:
        for image in soup.select(selector):
            gallery_ids.add(id(image))
            source = (
                image.get("data-src") or image.get("data-lazy-src") or image.get("data-original")
                or best_srcset(image.get("srcset", "")) or image.get("src")
            )
            if source:
                add(source, 90)
    # Plain <picture> layout is a lower-ranked fallback for Nieruchomości-online.
    for image in soup.find_all("img"):
        if id(image) in gallery_ids:
            continue
        source = (
            image.get("data-src") or image.get("data-lazy-src") or image.get("data-original")
            or best_srcset(image.get("srcset", "")) or image.get("src")
        )
        if source:
            add(source, 45 if image.get("data-src") or image.get("data-lazy-src") else 30)

    candidates.sort(key=lambda item: (-item[0], item[1]))
    photos, seen = [], set()
    for _, _, url in candidates:
        key = url.split("?", 1)[0]
        if key not in seen:
            seen.add(key)
            photos.append(url)
    return photos[:30]


def parse_listing_page(page_html: str, final_url: str) -> Listing:
    soup = BeautifulSoup(page_html, "html.parser")
    payloads = script_payloads(soup)
    description = extract_description(soup, payloads)
    price = extract_price(soup, payloads)
    return Listing(
        title=extract_title(soup, payloads) or "",
        price=price,
        czynsz=extract_czynsz(soup, description, price),
        description=description,
        photos=extract_photos(soup, payloads, final_url),
        url=final_url,
    )


def download_page(url: str) -> tuple[str, str]:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8"},
            timeout=(8, 25),
        )
    except requests.RequestException as error:
        raise ListingError("Не удалось открыть страницу. Попробуйте ещё раз.") from error
    if response.status_code in {404, 410}:
        raise ListingError("Это объявление больше недоступно.")
    if response.status_code >= 400:
        raise ListingError(f"Сайт вернул ошибку {response.status_code}. Попробуйте позже.")
    lower = response.text.lower()
    if any(marker in lower for marker in ("captcha", "verify you are human", "access denied", "unusual traffic")):
        raise ListingError("Сайт запросил проверку в браузере. Попробуйте немного позже.")
    final_url = clean_url(response.url) if is_supported_url(response.url) else url
    return response.text, final_url


def render_in_browser(url: str) -> tuple[str, str]:
    """Optional normal browser rendering; it does not bypass CAPTCHAs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise ListingError("Нужна обработка JavaScript. Установите Playwright по инструкции.") from error
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT, locale="pl-PL")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(1_500)
            result = (page.content(), page.url)
            context.close()
            browser.close()
            return result
    except Exception as error:
        raise ListingError("Не удалось отобразить страницу в браузере.") from error


def fetch_listing(url: str) -> Listing:
    url = clean_url(url)
    source, final_url = download_page(url)
    listing = parse_listing_page(source, final_url)
    # OLX can render parts of a page with JavaScript. Only try the optional
    # browser when normal HTML has missed a meaningful field.
    if not listing.usable() or not listing.description or not listing.photos:
        try:
            rendered_html, rendered_url = render_in_browser(final_url)
            rendered = parse_listing_page(rendered_html, clean_url(rendered_url))
            if rendered.usable():
                listing = Listing(
                    title=rendered.title or listing.title,
                    price=rendered.price or listing.price,
                    czynsz=rendered.czynsz or listing.czynsz,
                    description=max(listing.description or "", rendered.description or "", key=len) or None,
                    photos=(rendered.photos or listing.photos)[:30],
                    url=rendered.url,
                )
        except ListingError:
            if not listing.usable():
                raise
    if not listing.usable():
        raise ListingError("Не удалось прочитать название или цену объявления.")
    return listing


def listing_is_active(url: str) -> bool:
    try:
        page_html, _ = download_page(clean_url(url))
    except ListingError as error:
        return "больше недоступно" not in str(error).lower() and "404" not in str(error)
    text = normal_text(BeautifulSoup(page_html, "html.parser").get_text(" ", strip=True)).lower()
    closed_markers = (
        "to ogłoszenie nie jest już dostępne", "ogłoszenie wygasło", "ogłoszenie nieaktualne",
        "nie znaleziono ogłoszenia", "offer is no longer available",
    )
    return not any(marker in text for marker in closed_markers)


# =============================================================================
# Database
# =============================================================================

def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                price TEXT,
                czynsz TEXT,
                url TEXT NOT NULL,
                description TEXT,
                photos TEXT NOT NULL DEFAULT '[]',
                photo_msg_ids TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(listings)")}
        if "photos" not in columns:
            connection.execute("ALTER TABLE listings ADD COLUMN photos TEXT NOT NULL DEFAULT '[]'")
        if "photo_msg_ids" not in columns:
            connection.execute("ALTER TABLE listings ADD COLUMN photo_msg_ids TEXT NOT NULL DEFAULT '[]'")


def save_listing(owner_id: int, listing: Listing, photo_ids: list[int]) -> int:
    with database() as connection:
        connection.execute("DELETE FROM listings WHERE user_id = ? AND url = ?", (owner_id, listing.url))
        cursor = connection.execute(
            """
            INSERT INTO listings (user_id, title, price, czynsz, url, description, photos, photo_msg_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id, listing.title, listing.price, listing.czynsz, listing.url, listing.description,
                json.dumps(listing.photos, ensure_ascii=False), json.dumps(photo_ids),
            ),
        )
        return int(cursor.lastrowid)


def user_listings(owner_id: int) -> list[sqlite3.Row]:
    with database() as connection:
        return list(connection.execute("SELECT * FROM listings WHERE user_id = ? ORDER BY id", (owner_id,)))


def one_listing(item_id: int, owner_id: int) -> Optional[sqlite3.Row]:
    with database() as connection:
        return connection.execute(
            "SELECT * FROM listings WHERE id = ? AND user_id = ?", (item_id, owner_id)
        ).fetchone()


def replace_photo_ids(item_id: int, owner_id: int, photo_ids: list[int]) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE listings SET photo_msg_ids = ? WHERE id = ? AND user_id = ?",
            (json.dumps(photo_ids), item_id, owner_id),
        )


def remove_listing(item_id: int, owner_id: int) -> Optional[list[int]]:
    with database() as connection:
        row = connection.execute(
            "SELECT photo_msg_ids FROM listings WHERE id = ? AND user_id = ?", (item_id, owner_id)
        ).fetchone()
        if not row:
            return None
        connection.execute("DELETE FROM listings WHERE id = ? AND user_id = ?", (item_id, owner_id))
    try:
        return [int(value) for value in json.loads(row["photo_msg_ids"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


init_database()


# =============================================================================
# Telegram UI
# =============================================================================

def safe(value: Optional[str]) -> str:
    return html_lib.escape(value or "Не указано")


def shorten(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].strip()
    return (cut or text[:limit]).rstrip(".,;: ") + "…"


def card_text(
    title: str, price: Optional[str], czynsz: Optional[str], description: Optional[str], active: bool, expanded: bool
) -> str:
    state = "🟢 <b>Активно</b>" if active else "🔴 <b>Завершено / неактивно</b>"
    desc = description or "Описание отсутствует."
    desc = shorten(desc, 3_300 if expanded else 420)
    return (
        f"Статус: {state}\n\n🏠 <b>{safe(shorten(title, 300))}</b>\n"
        f"💰 <b>Аренда:</b> {safe(price)}\n"
        f"🏷 <b>Чинш / административные платежи:</b> {safe(czynsz)}\n\n"
        f"📌 <b>Описание:</b>\n{safe(desc)}"
    )


def card_keyboard(item_id: int, url: str, expanded: bool) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(
        "📖 Свернуть описание" if expanded else "📖 Показать описание",
        callback_data=f"toggle:{item_id}:{int(expanded)}",
    ))
    keyboard.add(types.InlineKeyboardButton("🔗 Открыть на сайте", url=url))
    keyboard.add(types.InlineKeyboardButton("🗑 Удалить из списка", callback_data=f"delete:{item_id}"))
    return keyboard


def saved_photos(raw_value: Optional[str]) -> list[str]:
    try:
        values = json.loads(raw_value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [value for value in values if isinstance(value, str) and value.startswith(("http://", "https://"))]


def download_photo(url: str, referer: str) -> Optional[io.BytesIO]:
    """Fallback if Telegram itself cannot fetch a hotlink-protected photo URL."""
    try:
        response = requests.get(
            url, headers={"Referer": referer, "User-Agent": USER_AGENT}, timeout=(6, 18), stream=True,
        )
        response.raise_for_status()
        if not response.headers.get("Content-Type", "").lower().startswith("image/"):
            return None
        content = response.content
        if not content or len(content) > 9_500_000:
            return None
        image = io.BytesIO(content)
        image.name = "listing-photo.jpg"
        return image
    except requests.RequestException:
        return None


def send_photos(chat_id: int, photos: list[str], listing_url: str) -> list[int]:
    photos = photos[:10]
    if not photos:
        return []
    try:
        messages = bot.send_media_group(chat_id, [types.InputMediaPhoto(url) for url in photos])
        return [message.message_id for message in messages]
    except Exception:
        # Telegram rejects a whole album if one photo is broken; preserve the rest.
        result: list[int] = []
        for url in photos:
            try:
                result.append(bot.send_photo(chat_id, url).message_id)
                continue
            except Exception:
                pass
            image = download_photo(url, listing_url)
            if image is None:
                continue
            try:
                result.append(bot.send_photo(chat_id, image).message_id)
            except Exception:
                pass
            finally:
                image.close()
        return result


# =============================================================================
# Handlers
# =============================================================================

@bot.message_handler(commands=["start", "help"])
def start(message: types.Message) -> None:
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("➕ Добавить", "📋 Список")
    bot.reply_to(message, "📥 Пришлите ссылку на OLX, Otodom или Nieruchomości-online.", reply_markup=keyboard)


@bot.message_handler(func=lambda message: bool(message.text and "Добавить" in message.text))
def add_button(message: types.Message) -> None:
    bot.send_message(message.chat.id, "Пришлите ссылку на квартиру.")


@bot.message_handler(func=lambda message: bool(message.text and ("Список" in message.text or message.text == "/list")))
def list_button(message: types.Message) -> None:
    owner_id = message.from_user.id
    rows = user_listings(owner_id)
    if not rows:
        bot.send_message(message.chat.id, "📋 Ваш список пока пуст.")
        return
    for row in rows:
        photo_ids = send_photos(message.chat.id, saved_photos(row["photos"]), row["url"])
        replace_photo_ids(row["id"], owner_id, photo_ids)
        bot.send_message(
            message.chat.id,
            card_text(row["title"], row["price"], row["czynsz"], row["description"], listing_is_active(row["url"]), False),
            reply_markup=card_keyboard(row["id"], row["url"], False),
        )


@bot.message_handler(func=lambda message: bool(message.text and message.text.strip().startswith(("http://", "https://"))))
def add_listing(message: types.Message) -> None:
    url = message.text.strip()
    if not is_supported_url(url):
        bot.reply_to(message, "❌ Поддерживаются только OLX, Otodom и Nieruchomości-online.")
        return
    progress = bot.send_message(message.chat.id, "⏳ Загружаю объявление…")
    try:
        listing = fetch_listing(url)
    except ListingError as error:
        bot.edit_message_text(f"❌ {safe(str(error))}", message.chat.id, progress.message_id)
        return
    except Exception:
        bot.edit_message_text("❌ Не удалось обработать ссылку. Попробуйте позже.", message.chat.id, progress.message_id)
        return
    try:
        bot.delete_message(message.chat.id, progress.message_id)
    except Exception:
        pass

    owner_id = message.from_user.id
    photo_ids = send_photos(message.chat.id, listing.photos, listing.url)
    item_id = save_listing(owner_id, listing, photo_ids)
    bot.send_message(
        message.chat.id,
        card_text(listing.title, listing.price, listing.czynsz, listing.description, True, False),
        reply_markup=card_keyboard(item_id, listing.url, False),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("toggle:"))
def toggle_description(call: types.CallbackQuery) -> None:
    try:
        _, item_as_text, expanded_as_text = call.data.split(":", 2)
        item_id, was_expanded = int(item_as_text), bool(int(expanded_as_text))
    except (ValueError, AttributeError):
        bot.answer_callback_query(call.id, "Некорректная команда.")
        return
    row = one_listing(item_id, call.from_user.id)
    if not row:
        bot.answer_callback_query(call.id, "Объявление не найдено в вашем списке.")
        return
    expanded = not was_expanded
    try:
        bot.edit_message_text(
            card_text(row["title"], row["price"], row["czynsz"], row["description"], listing_is_active(row["url"]), expanded),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=card_keyboard(item_id, row["url"], expanded),
        )
    except Exception:
        bot.answer_callback_query(call.id, "Не удалось обновить сообщение.")
        return
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delete:"))
def delete_listing(call: types.CallbackQuery) -> None:
    try:
        item_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Некорректная команда.")
        return
    photo_ids = remove_listing(item_id, call.from_user.id)
    if photo_ids is None:
        bot.answer_callback_query(call.id, "Объявление не найдено в вашем списке.")
        return
    for message_id in photo_ids:
        try:
            bot.delete_message(call.message.chat.id, message_id)
        except Exception:
            pass
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Объявление удалено из списка.")


if __name__ == "__main__":
    print("Bot started")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
