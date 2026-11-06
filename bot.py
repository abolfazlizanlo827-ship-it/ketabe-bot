import queue
import threading
import requests
import re
import time
import json
import os
import sys
import traceback
import html

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag


# =========================================
# 1. SETTINGS
# =========================================

TOKEN = ""

API = f"https://api.telegram.org/bot{TOKEN}/"
SHOP = "https://shop.mehromah.ir/"

STATE_FILE = "/storage/emulated/0/ketabyab_state.json"
LOG_FILE = "/storage/emulated/0/ketabyab_log.txt"

REQUEST_TIMEOUT = (15, 35)
MAX_RETRIES = 3
MAX_LOG_SIZE = 500000
MAX_SEARCH_PAGES = 20

PUBLISHERS = {
    1: "مهر و ماه",
    2: "خیلی سبز",
    3: "مبتکران",
    4: "گاج",
    5: "نهنگ",
    6: "ناشر شماره ۶",
    7: "ناشر شماره ۷",
    8: "ناشر شماره ۸",
    9: "ناشر شماره ۹",
    10: "ناشر شماره ۱۰",
    11: "ناشر شماره ۱۱",
    12: "ناشر شماره ۱۲",
    13: "ناشر شماره ۱۳",
    14: "ناشر شماره ۱۴",
    15: "ناشر شماره ۱۵",
    16: "ناشر شماره ۱۶",
    17: "ناشر شماره ۱۷",
    18: "ناشر شماره ۱۸",
    19: "ناشر شماره ۱۹",
    20: "ناشر شماره ۲۰"
}


# =========================================
# 2. LOGGING - EARLY INITIALIZATION
# =========================================

def log_error(message):

    message = str(message)

    print(message, flush=True)

    try:
        if os.path.exists(LOG_FILE):
            if os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
                old_file = LOG_FILE + ".old"

                if os.path.exists(old_file):
                    os.remove(old_file)

                os.replace(LOG_FILE, old_file)

        with open(LOG_FILE, "a", encoding="utf-8") as file:
            file.write(
                "\n\n" +
                time.strftime("%Y-%m-%d %H:%M:%S") +
                "\n"
            )
            file.write(message)
            file.flush()
            os.fsync(file.fileno())

    except Exception as error:
        print("LOG WRITE ERROR:", repr(error), flush=True)


def initialize_log():

    try:
        folder = os.path.dirname(LOG_FILE)

        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)

        with open(LOG_FILE, "a", encoding="utf-8") as file:
            file.write(
                "\n\n========== BOT STARTING ==========\n"
            )
            file.write(
                time.strftime("%Y-%m-%d %H:%M:%S") + "\n"
            )
            file.write("Log initialization successful.\n")
            file.flush()

        print("LOG FILE:", LOG_FILE, flush=True)
        print("LOG INITIALIZED", flush=True)

    except Exception:
        print("LOG INITIALIZATION FAILED", flush=True)
        print(traceback.format_exc(), flush=True)


def global_exception_handler(exc_type, exc_value, exc_tb):

    error_text = "".join(
        traceback.format_exception(
            exc_type,
            exc_value,
            exc_tb
        )
    )

    log_error("UNHANDLED PYTHON ERROR:\n" + error_text)


sys.excepthook = global_exception_handler

initialize_log()


# =========================================
# 3. SESSIONS
# =========================================

tg = requests.Session()
shop = requests.Session()
tg_worker_local = threading.local()
search_queue = queue.Queue(maxsize=100)
tg.headers.update({
    "User-Agent": "KetabYabBot/3.0"
})

shop.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"
})


# =========================================
# 4. NETWORK RETRY
# =========================================

def request_retry(
    session,
    method,
    url,
    attempts=MAX_RETRIES,
    **kwargs
):

    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    last_error = None

    for attempt in range(attempts):

        try:
            response = session.request(
                method,
                url,
                **kwargs
            )

            if response.status_code in (429, 500, 502, 503, 504):

                if response.status_code == 429:
                    try:
                        wait = int(
                            response.json()
                            .get("parameters", {})
                            .get("retry_after", 5)
                        )
                    except Exception:
                        wait = 5
                else:
                    wait = 2 ** attempt

                if attempt < attempts - 1:
                    print(
                        "Temporary HTTP error:",
                        response.status_code,
                        flush=True
                    )
                    time.sleep(min(wait, 30))
                    continue

            response.raise_for_status()
            return response

        except requests.exceptions.RequestException as error:

            last_error = error

            print(
                f"Request error {attempt + 1}/{attempts}: {error}",
                flush=True
            )

            if attempt < attempts - 1:
                time.sleep(min(2 ** (attempt + 1), 15))

    if last_error:
        raise last_error

    raise RuntimeError("Request failed.")


# =========================================
# 5. STATE MANAGEMENT
# =========================================

user_publishers = {}
offset = 0


def save_state():

    data = {
        "publishers": user_publishers,
        "offset": offset
    }

    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, STATE_FILE)


def load_state():

    global user_publishers, offset

    if not os.path.exists(STATE_FILE):
        print("No previous state file.", flush=True)
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        publishers = data.get("publishers", {})

        if isinstance(publishers, dict):
            user_publishers = publishers

        offset = max(0, int(data.get("offset", 0)))

        print("Publishers loaded:", user_publishers, flush=True)
        print("Offset loaded:", offset, flush=True)

    except Exception:
        log_error(
            "STATE FILE ERROR:\n" +
            traceback.format_exc()
        )


# =========================================
# 6. DIGIT CONVERSION
# =========================================

def digits(text):

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return str(text).translate(table)


# =========================================
# 7. ISBN NORMALIZATION
# =========================================

def normalize_isbn(text):

    text = digits(text).upper()

    return re.sub(r"[^0-9X]", "", text)


# =========================================
# 8. ISBN VALIDATION
# =========================================

def valid_isbn(isbn):

    isbn = normalize_isbn(isbn)

    if len(isbn) == 13:

        if not isbn.isdigit():
            return False

        if not isbn.startswith(("978", "979")):
            return False

        total = 0

        for index, digit in enumerate(isbn[:12]):
            weight = 1 if index % 2 == 0 else 3
            total += int(digit) * weight

        check = (10 - total % 10) % 10

        return check == int(isbn[-1])

    if len(isbn) == 10:

        if not re.fullmatch(r"[0-9]{9}[0-9X]", isbn):
            return False

        total = 0

        for index, char in enumerate(isbn):
            value = 10 if char == "X" else int(char)
            total += value * (10 - index)

        return total % 11 == 0

    return False


# =========================================
# 9. ISBN CONVERSION
# =========================================

def isbn_variants(isbn):

    isbn = normalize_isbn(isbn)
    variants = {isbn}

    # ISBN-10 -> ISBN-13
    if len(isbn) == 10:

        base = "978" + isbn[:9]

        total = sum(
            int(d) * (1 if i % 2 == 0 else 3)
            for i, d in enumerate(base)
        )

        check = str((10 - total % 10) % 10)

        variants.add(base + check)

    # ISBN-13 -> ISBN-10
    elif len(isbn) == 13 and isbn.startswith("978"):

        base = isbn[3:12]

        total = sum(
            int(d) * (10 - i)
            for i, d in enumerate(base)
        )

        check = (-total) % 11

        check_char = "X" if check == 10 else str(check)

        variants.add(base + check_char)

    return variants


# =========================================
# 10. PRICE EXTRACTION
# =========================================

def parse_price(value):

    if value is None:
        return None

    value = digits(str(value))
    number = re.sub(r"\D", "", value)

    if not number:
        return None

    try:
        return int(number)
    except ValueError:
        return None


def get_price(element):

    if not element:
        return None

    content = element.get("content")

    if content:
        price = parse_price(content)
        if price is not None:
            return price

    value = element.get("data-price")

    if value:
        price = parse_price(value)
        if price is not None:
            return price

    return parse_price(
        element.get_text(" ", strip=True)
    )


# =========================================
# 11. JSON-LD PRICE EXTRACTION
# =========================================

def get_jsonld_prices(soup):

    prices = []

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    def scan(data):

        if isinstance(data, list):
            for item in data:
                scan(item)

        elif isinstance(data, dict):

            if "price" in data:
                parsed = parse_price(data.get("price"))

                if parsed is not None and parsed > 0:
                    prices.append(parsed)

            offers = data.get("offers")

            if offers is not None:
                scan(offers)

            graph = data.get("@graph")

            if graph is not None:
                scan(graph)

    for script in scripts:

        try:
            raw = script.string or script.get_text()
            data = json.loads(raw)
            scan(data)

        except Exception:
            continue

    return prices


# =========================================
# 12. PRODUCT PRICES
# =========================================

def extract_prices(soup):

    old_selectors = [
        "#old_price_display",
        ".old-price",
        ".regular-price",
        "[data-old-price]"
    ]

    new_selectors = [
        "#our_price_display",
        ".current-price",
        ".product-price",
        "[itemprop='price']",
        "meta[property='product:price:amount']"
    ]

    old_price = None
    new_price = None

    for selector in old_selectors:

        element = soup.select_one(selector)
        old_price = get_price(element)

        if old_price:
            break

    for selector in new_selectors:

        element = soup.select_one(selector)
        new_price = get_price(element)

        if new_price:
            break

    json_prices = get_jsonld_prices(soup)

    if new_price is None and json_prices:
        new_price = json_prices[0]

    discount = None

    if (
        old_price is not None
        and new_price is not None
        and old_price > new_price
    ):

        discount = round(
            (old_price - new_price) * 100 / old_price
        )

    return old_price, new_price, discount


# =========================================
# 13. FORMAT PRICE
# =========================================

def format_price(number):

    if number is None:
        return "نامشخص"

    return f"{number:,} تومان"


# =========================================
# 14. TELEGRAM API
# =========================================

def telegram_call(method, data=None, params=None):

    data = data or {}
    params = params or {}

    telegram_session = getattr(tg_worker_local, "session", tg)

    response = request_retry(
        telegram_session,
        "POST" if data else "GET",
        API + method,
        attempts=3,
        data=data if data else None,
        params=params if params else None
    )

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(
            "Telegram returned invalid JSON: " +
            response.text[:500]
        )

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {result}"
        )

    return result
def send(chat_id, message):

    if not message:
        return

    message = str(message)

    if len(message) > 4000:
        message = message[:3900] + "\n\n... ادامه پیام حذف شد."

    telegram_call(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True
        }
    )


def get_updates(current_offset):

    return telegram_call(
        "getUpdates",
        params={
            "offset": current_offset,
            "timeout": 25,
            "allowed_updates": json.dumps(["message"])
        }
    )


# =========================================
# 15. TELEGRAM POLLING SETUP
# =========================================

def prepare_polling():

    info = telegram_call("getWebhookInfo")

    webhook_url = info.get("result", {}).get("url", "")

    if webhook_url:

        print("Active webhook found. Removing...", flush=True)

        telegram_call(
            "deleteWebhook",
            data={
                "drop_pending_updates": False
            }
        )

        print("Webhook removed.", flush=True)


# =========================================
# 16. PUBLISHER MENU
# =========================================

def publisher_menu():

    message = "📚 انتخاب ناشر\n\n"

    for number, name in PUBLISHERS.items():
        message += f"{number} - {name}\n"

    message += "\nعدد ناشر موردنظر را ارسال کن."

    return message


# =========================================
# 17. PRODUCT LINK DISCOVERY
# =========================================

def get_product_links(soup, base_url, isbn):

    links = []
    variants = isbn_variants(isbn)

    for a in soup.find_all("a", href=True):

        link = urljoin(base_url, a["href"])
        link, _ = urldefrag(link)

        parsed = urlparse(link)
        hostname = parsed.netloc.lower().split(":")[0]

        if hostname not in (
            "shop.mehromah.ir",
            "www.shop.mehromah.ir"
        ):
            continue

        if not parsed.path.lower().endswith(".html"):
            continue

        if link not in links:
            links.append(link)

    def score(link):

        normalized = normalize_isbn(
            urlparse(link).path
        )

        return any(
            variant in normalized
            for variant in variants
        )

    links.sort(key=score, reverse=True)

    return links[:MAX_SEARCH_PAGES]


# =========================================
# 18. CHECK ISBN IN PRODUCT PAGE
# =========================================

def page_has_isbn(soup, url, isbn):

    variants = isbn_variants(isbn)

    text = digits(
        soup.get_text(" ", strip=True)
    )

    # بررسی متن صفحه با جداکننده‌های معمول ISBN
    for variant in variants:

        pattern = r"(?<!\d)" + re.escape(variant) + r"(?!\d)"

        if re.search(pattern, text):
            return True

    compact_url = normalize_isbn(
        urlparse(url).path
    )

    for variant in variants:
        if variant in compact_url:
            return True

    for element in soup.find_all(True):

        for attribute in (
            "content",
            "value",
            "data-isbn",
            "data-ean"
        ):

            value = element.get(attribute)

            if value:

                normalized = normalize_isbn(value)

                if normalized in variants:
                    return True

    return False


# =========================================
# 19. PRODUCT TITLE
# =========================================

def get_product_title(soup):

    selectors = [
        "h1",
        "meta[property='og:title']",
        "meta[name='title']"
    ]

    for selector in selectors:

        element = soup.select_one(selector)

        if not element:
            continue

        if element.name == "meta":
            title = element.get("content", "")
        else:
            title = element.get_text(" ", strip=True)

        if title.strip():
            return html.unescape(title.strip())

    return "نام کتاب پیدا نشد"


# =========================================
# 20. MEHROMAH SEARCH
# =========================================

def find_mehromah_book(isbn):

    isbn = normalize_isbn(isbn)

    search_url = urljoin(SHOP, "search")

    params = {
        "controller": "search",
        "orderby": "position",
        "orderway": "desc",
        "search_query": isbn,
        "submit_search": ""
    }

    print("\n========== MEHROMAH SEARCH ==========", flush=True)
    print("ISBN:", isbn, flush=True)

    response = request_retry(
        shop,
        "GET",
        search_url,
        params=params
    )

    search_soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    links = get_product_links(
        search_soup,
        response.url,
        isbn
    )

    print("PRODUCT LINKS:", len(links), flush=True)

    if not links:
        return None

    for index, link in enumerate(links, 1):

        try:

            print(
                f"Checking product {index}/{len(links)}",
                flush=True
            )

            page = request_retry(
                shop,
                "GET",
                link
            )

            product = BeautifulSoup(
                page.text,
                "html.parser"
            )

            if not page_has_isbn(product, page.url, isbn):
                print("ISBN NOT MATCHED", flush=True)
                continue

            title = get_product_title(product)

            old_price, new_price, discount = extract_prices(product)

            print("BOOK FOUND:", title, flush=True)
            print("OLD PRICE:", old_price, flush=True)
            print("NEW PRICE:", new_price, flush=True)

            return {
                "title": title,
                "old": old_price,
                "new": new_price,
                "discount": discount,
                "link": page.url
            }

        except Exception:

            log_error(
                "PRODUCT PAGE ERROR:\n" +
                traceback.format_exc()
            )

            continue

    return None


# =========================================
# 21. SEARCH RESPONSE
# =========================================

def search_mehromah(isbn):

    book = find_mehromah_book(isbn)

    if not book:

        return (
            "❌ این ISBN در نتایج سایت مهر و ماه پیدا نشد.\n\n"
            "ممکن است کتاب در سایت موجود نباشد "
            "یا موتور جستجوی سایت آن را نمایش نداده باشد."
        )

    message = (
        "📚 نتیجه جستجو\n\n"
        "ناشر: مهر و ماه\n"
        f"نام کتاب: {book['title']}\n"
        f"ISBN: {isbn}\n\n"
        f"قیمت اصلی: {format_price(book['old'])}\n"
        f"قیمت فعلی: {format_price(book['new'])}\n"
    )

    if book["discount"] is not None:
        message += f"تخفیف: {book['discount']}%\n"

    message += "\nلینک محصول:\n" + book["link"]

    return message


# =========================================
# 22. USER MESSAGE HANDLER
# =========================================
def search_worker():

    tg_worker_local.session = requests.Session()
    tg_worker_local.session.headers.update({
        "User-Agent": "KetabYabBot/3.0"
    })

    print("SEARCH WORKER STARTED", flush=True)

    while True:

        job = search_queue.get()

        try:
            chat_id, isbn = job

            send(chat_id, "🔎 در حال جستجوی کتاب...")

            answer = search_mehromah(isbn)

            send(chat_id, answer)

        except Exception:

            log_error(
                "SEARCH WORKER ERROR:\n" +
                traceback.format_exc()
            )

            try:
                send(
                    chat_id,
                    "⚠️ جستجوی این کتاب با خطا مواجه شد. ربات فعال است."
                )
            except Exception:
                log_error(traceback.format_exc())

        finally:
            search_queue.task_done()


def start_search_worker():

    worker = threading.Thread(
        target=search_worker,
        daemon=True,
        name="BookSearchWorker"
    )

    worker.start()
def handle_message(chat_id, raw_text):

    text = digits(raw_text.strip())
    user_id = str(chat_id)

    # LOG
    if text == "/log":

        if not os.path.exists(LOG_FILE):
            send(
                chat_id,
                "فایل گزارش پیدا نشد.\nمسیر مورد انتظار:\n" + LOG_FILE
            )
            return

        try:
            with open(LOG_FILE, "r", encoding="utf-8") as file:
                error_log = file.read()

            if not error_log.strip():
                send(chat_id, "گزارش خطا خالی است.")
            else:
                send(
                    chat_id,
                    "📋 گزارش خطای ربات:\n\n" +
                    error_log[-3000:]
                )

        except Exception:
            log_error(
                "READ LOG ERROR:\n" +
                traceback.format_exc()
            )
            send(chat_id, "خواندن فایل گزارش با خطا مواجه شد.")

        return

    # STATUS
    if text == "/status":

        selected = user_publishers.get(user_id)

        if selected is not None:
            publisher_name = PUBLISHERS.get(
                int(selected),
                "نامشخص"
            )
        else:
            publisher_name = "انتخاب نشده"

        send(
            chat_id,
            "✅ ربات فعال است.\n\n"
            f"ناشر انتخاب‌شده: {publisher_name}\n"
            f"Offset: {offset}\n"
            f"فایل گزارش: {LOG_FILE}"
        )

        return

    # START / MENU
    if text in ("/start", "شروع", "ناشرها", "منو"):

        selected = user_publishers.get(user_id)

        if selected is None:
            send(chat_id, publisher_menu())
        else:
            selected = int(selected)

            send(
                chat_id,
                f"ناشر فعلی: {PUBLISHERS[selected]}\n\n"
                "برای تغییر ناشر، عدد دیگری از ۱ تا ۲۰ بفرست.\n"
                "برای جستجو، بارکد کتاب را اسکن کن."
            )

        return

    # PUBLISHER SELECTION
    if text.isdigit() and 1 <= int(text) <= 20:

        number = int(text)

        user_publishers[user_id] = number
        save_state()

        name = PUBLISHERS[number]

        if number == 1:
            send(
                chat_id,
                f"✅ ناشر انتخاب شد: {name}\n\n"
                "حالا بارکد کتاب‌ها را پشت سر هم اسکن کن.\n"
                "برای تغییر ناشر، عدد دیگری بفرست."
            )
        else:
            send(
                chat_id,
                f"✅ ناشر انتخاب شد: {name}\n\n"
                "⏳ جستجوی این ناشر هنوز فعال نشده است.\n"
                "فعلاً مهر و ماه فعال است."
            )

        return

    # ISBN
    isbn = normalize_isbn(text)

    if len(isbn) not in (10, 13):
        send(
            chat_id,
            "⚠️ بارکد معتبر نیست.\n\n"
            "لطفاً ISBN ده یا سیزده رقمی کتاب را بفرست."
        )
        return

    if not valid_isbn(isbn):
        send(
            chat_id,
            "⚠️ رقم کنترل بارکد معتبر نیست.\n\n"
            "لطفاً بارکد را دوباره اسکن کن."
        )
        return

    # PUBLISHER CHECK
    publisher = user_publishers.get(user_id)

    if publisher is None:
        send(
            chat_id,
            "⚠️ هنوز ناشری انتخاب نکردی.\n\n" +
            publisher_menu()
        )
        return

    publisher = int(publisher)

    # MEHROMAH
        # MEHROMAH
    if publisher == 1:

        try:
            search_queue.put_nowait((chat_id, isbn))

            send(
                chat_id,
                "📥 بارکد دریافت شد و در صف جستجو قرار گرفت.\n"
                "می‌توانی بارکد بعدی را هم بفرستی."
            )

        except queue.Full:

            send(
                chat_id,
                "⚠️ صف جستجو پر است. کمی صبر کن."
            )

        except Exception:

            log_error(
                "QUEUE ERROR:\n" +
                traceback.format_exc()
            )

            send(
                chat_id,
                "خطا در ثبت بارکد."
            )

        return

    # OTHER PUBLISHERS
    send(
        chat_id,
        f"⏳ جستجوی {PUBLISHERS[publisher]} هنوز فعال نشده است."
    )


# =========================================
# 23. PROCESS UPDATES
# =========================================

def process_updates(updates):

    global offset

    for update in updates:

        update_id = update.get("update_id")

        if update_id is None:
            continue

        try:

            message = update.get("message")

            if message:

                chat_id = message.get(
                    "chat", {}
                ).get("id")

                text = message.get("text", "")

                if chat_id is not None and text:

                    try:
                        handle_message(chat_id, text)

                    except Exception:

                        log_error(
                            "MESSAGE HANDLING ERROR:\n" +
                            traceback.format_exc()
                        )

                        try:
                            send(
                                chat_id,
                                "⚠️ خطای داخلی رخ داد.\n"
                                "ربات همچنان فعال است؛ دوباره تلاش کن."
                            )
                        except Exception:
                            log_error(
                                "TELEGRAM ERROR MESSAGE FAILED:\n" +
                                traceback.format_exc()
                            )

        except Exception:

            log_error(
                "UPDATE PROCESSING ERROR:\n" +
                traceback.format_exc()
            )

        finally:

            offset = update_id + 1

            try:
                save_state()
            except Exception:
                log_error(
                    "SAVE STATE ERROR:\n" +
                    traceback.format_exc()
                )


# =========================================
# 24. STARTUP
# =========================================

def main():

    global offset

    log_error("MAIN FUNCTION STARTED")

    if not TOKEN or TOKEN == "توکن_جدید_ربات":

        raise ValueError(
            "ابتدا توکن جدید ربات را در قسمت TOKEN وارد کن."
        )

    load_state()
start_search_worker()
    try:
        prepare_polling()
    except Exception:
        log_error(
            "WEBHOOK SETUP ERROR:\n" +
            traceback.format_exc()
        )

    print("=" * 40, flush=True)
    print("KETABYAB BOT V3 STARTED", flush=True)
    print("Publisher settings loaded.", flush=True)
    print("ISBN validation enabled.", flush=True)
    print("Mehromah search enabled.", flush=True)
    print("Log file:", LOG_FILE, flush=True)
    print("=" * 40, flush=True)

    log_error("BOT IS RUNNING")

    error_wait = 5
    max_wait = 60

    while True:

        try:

            result = get_updates(offset)

            error_wait = 5

            updates = result.get("result", [])

            if updates:
                process_updates(updates)

        except KeyboardInterrupt:

            log_error("BOT STOPPED BY USER")
            print("Bot stopped by user.", flush=True)
            break

        except Exception:

            log_error(
                "MAIN LOOP ERROR:\n" +
                traceback.format_exc()
            )

            print(
                f"Connection problem. Retry in {error_wait} seconds.",
                flush=True
            )

            time.sleep(error_wait)

            error_wait = min(
                error_wait * 2,
                max_wait
            )


# =========================================
# 25. RUN
# =========================================

if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        log_error("PROGRAM INTERRUPTED")

    except BaseException:
        log_error(
            "FATAL PROGRAM ERROR:\n" +
            traceback.format_exc()
        )
        print(
            "FATAL ERROR SAVED TO:",
            LOG_FILE,
            flush=True
        )
