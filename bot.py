
import os
import re
import sys
import json
import time
import html
import threading
import traceback
import requests
import sqlite3
import contextlib

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
DB_FILE = "/storage/emulated/0/ketabyab_jobs.sqlite3"

REQUEST_TIMEOUT = (20, 60)
MAX_RETRIES = 5
MAX_LOG_SIZE = 500000
MAX_SEARCH_PAGES = 8

WORKER_COUNT = 3
JOB_POLL_SECONDS = 2
JOB_LEASE_SECONDS = 1800

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
# 2. LOGGING
# =========================================

def log_error(message):
    message = str(message)
    print(message, flush=True)

    try:
        folder = os.path.dirname(LOG_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)

        if os.path.exists(LOG_FILE):
            if os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
                old_file = LOG_FILE + ".old"

                if os.path.exists(old_file):
                    os.remove(old_file)

                os.replace(LOG_FILE, old_file)

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                "\n\n" +
                time.strftime("%Y-%m-%d %H:%M:%S") +
                "\n"
            )
            f.write(message)
            f.flush()
            os.fsync(f.fileno())

    except Exception as e:
        print("LOG ERROR:", repr(e), flush=True)


def initialize_log():
    try:
        folder = os.path.dirname(LOG_FILE)

        if folder:
            os.makedirs(folder, exist_ok=True)

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n========== BOT STARTING ==========\n")
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.flush()

    except Exception:
        print(traceback.format_exc(), flush=True)


sys.excepthook = lambda t, v, tb: log_error(
    "".join(traceback.format_exception(t, v, tb))
)

initialize_log()


# =========================================
# 3. THREADS AND SESSIONS
# =========================================

tg_local = threading.local()
shop_local = threading.local()

main_session = requests.Session()

main_session.headers.update({
    "User-Agent": "KetabYabBot/5.0"
})


def get_telegram_session():
    session = getattr(tg_local, "session", None)
    return session if session else main_session


def get_shop_session():
    session = getattr(shop_local, "session", None)
    return session if session else main_session


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

            if response.status_code in (
                429, 500, 502, 503, 504
            ):

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
                    wait = min(2 ** attempt, 30)

                if attempt < attempts - 1:
                    log_error(
                        f"HTTP {response.status_code}; retry in {wait}s"
                    )
                    time.sleep(min(wait, 60))
                    continue

            response.raise_for_status()
            return response

        except requests.exceptions.RequestException as e:

            last_error = e

            log_error(
                f"NETWORK ERROR {attempt + 1}/{attempts}: {repr(e)}"
            )

            if attempt < attempts - 1:
                time.sleep(min(2 ** (attempt + 1), 15))

    if last_error:
        raise last_error

    raise RuntimeError("Network request failed")


# =========================================
# 5. STATE
# =========================================

user_publishers = {}
offset = 0
state_lock = threading.Lock()


def save_state():

    data = {
        "publishers": user_publishers,
        "offset": offset
    }

    temp_file = STATE_FILE + ".tmp"

    try:
        with state_lock:

            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    indent=2
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_file, STATE_FILE)

    except Exception:
        log_error(
            "SAVE STATE ERROR:\n" +
            traceback.format_exc()
        )


def load_state():

    global user_publishers, offset

    if not os.path.exists(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        publishers = data.get("publishers", {})

        if isinstance(publishers, dict):
            user_publishers = publishers

        offset = max(0, int(data.get("offset", 0)))

        print("STATE LOADED", flush=True)

    except Exception:
        log_error(
            "LOAD STATE ERROR:\n" +
            traceback.format_exc()
        )


# =========================================
# 6. PERSISTENT SQLITE QUEUE
# =========================================

def db_connect():

    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


@contextlib.contextmanager
def db_session():

    conn = db_connect()

    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def initialize_job_db():

    folder = os.path.dirname(DB_FILE)

    if folder:
        os.makedirs(folder, exist_ok=True)

    with db_session() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                isbn TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_run REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_ready
            ON jobs(status, next_run)
        """)

    log_error(
        "PERSISTENT DATABASE READY: " + DB_FILE
    )


def enqueue_job(chat_id, isbn):

    now = time.time()

    with db_session() as conn:

        conn.execute(
            """
            INSERT INTO jobs
            (chat_id, isbn, status, attempts, next_run, updated)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (
                str(chat_id),
                isbn,
                "pending",
                now,
                now
            )
        )


def pending_jobs_count():

    with db_session() as conn:

        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE status IN ('pending', 'running')
            """
        ).fetchone()

        return int(row[0] or 0)


def claim_job():

    now = time.time()

    with db_session() as conn:

        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT id, chat_id, isbn, attempts
            FROM jobs
            WHERE
                (status='pending' AND next_run<=?)
                OR
                (status='running' AND updated<?)
            ORDER BY id
            LIMIT 1
            """,
            (
                now,
                now - JOB_LEASE_SECONDS
            )
        ).fetchone()

        if row is None:
            return None

        job_id, chat_id, isbn, attempts = row

        conn.execute(
            """
            UPDATE jobs
            SET status='running', updated=?
            WHERE id=?
            """,
            (now, job_id)
        )

        return (
            int(job_id),
            int(chat_id),
            isbn,
            int(attempts)
        )


def finish_job(job_id):

    with db_session() as conn:
        conn.execute(
            "DELETE FROM jobs WHERE id=?",
            (job_id,)
        )


def retry_job(job_id, attempts, error):

    count = attempts + 1

    delay = min(
        300,
        5 * (2 ** min(count - 1, 6))
    )

    now = time.time()

    with db_session() as conn:

        conn.execute(
            """
            UPDATE jobs
            SET
                status='pending',
                attempts=?,
                next_run=?,
                updated=?,
                last_error=?
            WHERE id=?
            """,
            (
                count,
                now + delay,
                now,
                str(error)[:1000],
                job_id
            )
        )

    log_error(
        "JOB " + str(job_id) +
        " RETRY #" + str(count) +
        " IN " + str(delay) + " SECONDS"
    )


# =========================================
# 7. ISBN
# =========================================

def digits(value):

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return str(value).translate(table)


def normalize_isbn(value):

    value = digits(value).upper()

    return re.sub(r"[^0-9X]", "", value)


def valid_isbn(isbn):

    isbn = normalize_isbn(isbn)

    if len(isbn) == 13:

        if not isbn.isdigit():
            return False

        if not isbn.startswith(("978", "979")):
            return False

        total = sum(
            int(d) * (1 if i % 2 == 0 else 3)
            for i, d in enumerate(isbn[:12])
        )

        return (
            (10 - total % 10) % 10
            == int(isbn[-1])
        )

    if len(isbn) == 10:

        if not re.fullmatch(
            r"[0-9]{9}[0-9X]",
            isbn
        ):
            return False

        total = sum(
            (10 if c == "X" else int(c)) * (10 - i)
            for i, c in enumerate(isbn)
        )

        return total % 11 == 0

    return False


def isbn_variants(isbn):

    isbn = normalize_isbn(isbn)
    variants = {isbn}

    if len(isbn) == 10:

        base = "978" + isbn[:9]

        total = sum(
            int(d) * (1 if i % 2 == 0 else 3)
            for i, d in enumerate(base)
        )

        variants.add(
            base + str((10 - total % 10) % 10)
        )

    elif len(isbn) == 13 and isbn.startswith("978"):

        base = isbn[3:12]

        total = sum(
            int(d) * (10 - i)
            for i, d in enumerate(base)
        )

        check = (-total) % 11

        variants.add(
            base + ("X" if check == 10 else str(check))
        )

    return variants


# =========================================
# 8. PRICE EXTRACTION
# =========================================

def parse_price(value):

    if value is None:
        return None

    number = re.sub(
        r"\D",
        "",
        digits(str(value))
    )

    if not number:
        return None

    try:
        return int(number)
    except ValueError:
        return None


def get_price(element):

    if not element:
        return None

    for attribute in ("content", "data-price"):

        value = element.get(attribute)

        if value:
            result = parse_price(value)

            if result is not None:
                return result

    return parse_price(
        element.get_text(" ", strip=True)
    )


def get_jsonld_prices(soup):

    prices = []

    def scan(data):

        if isinstance(data, list):

            for item in data:
                scan(item)

        elif isinstance(data, dict):

            if "price" in data:

                price = parse_price(data.get("price"))

                if price and price > 0:
                    prices.append(price)

            if data.get("offers") is not None:
                scan(data["offers"])

            if data.get("@graph") is not None:
                scan(data["@graph"])

    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):

        try:
            raw = script.string or script.get_text()
            scan(json.loads(raw))

        except Exception:
            continue

    return prices


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

        old_price = get_price(
            soup.select_one(selector)
        )

        if old_price:
            break

    for selector in new_selectors:

        new_price = get_price(
            soup.select_one(selector)
        )

        if new_price:
            break

    json_prices = get_jsonld_prices(soup)

    if new_price is None and json_prices:
        new_price = json_prices[0]

    discount = None

    if (
        old_price
        and new_price
        and old_price > new_price
    ):

        discount = round(
            (old_price - new_price) * 100 / old_price
        )

    return old_price, new_price, discount


def format_price(number):

    if number is None:
        return "نامشخص"

    return f"{number:,} تومان"


# =========================================
# 9. TELEGRAM
# =========================================

def telegram_call(method, data=None, params=None):

    session = get_telegram_session()

    response = request_retry(
        session,
        "POST" if data else "GET",
        API + method,
        attempts=3,
        data=data,
        params=params
    )

    try:
        result = response.json()

    except Exception:
        raise RuntimeError(
            "Telegram returned invalid JSON"
        )

    if not result.get("ok"):
        raise RuntimeError(str(result))

    return result


def send(chat_id, message):

    if not message:
        return

    message = str(message)

    if len(message) > 4000:
        message = (
            message[:3900] +
            "\n\nادامه پیام کوتاه شد."
        )

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


def prepare_polling():

    info = telegram_call("getWebhookInfo")

    webhook = info.get(
        "result", {}
    ).get("url", "")

    if webhook:

        telegram_call(
            "deleteWebhook",
            data={
                "drop_pending_updates": False
            }
        )


# =========================================
# 10. MEHROMAH SEARCH
# =========================================

def get_product_links(soup, base_url, isbn):

    links = []

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

    variants = isbn_variants(isbn)

    def score(link):

        path = normalize_isbn(
            urlparse(link).path
        )

        return any(
            v in path
            for v in variants
        )

    links.sort(key=score, reverse=True)

    return links[:MAX_SEARCH_PAGES]


def page_has_isbn(soup, url, isbn):

    variants = isbn_variants(isbn)

    text = digits(
        soup.get_text(" ", strip=True)
    )

    for variant in variants:

        pattern = (
            r"(?<!\d)" +
            re.escape(variant) +
            r"(?!\d)"
        )

        if re.search(pattern, text):
            return True

    path = normalize_isbn(
        urlparse(url).path
    )

    for variant in variants:

        if variant in path:
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

                if normalize_isbn(value) in variants:
                    return True

    return False


def get_product_title(soup):

    for selector in (
        "h1",
        "meta[property='og:title']",
        "meta[name='title']"
    ):

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


def find_mehromah_book(isbn):

    session = get_shop_session()

    search_url = urljoin(SHOP, "search")

    params = {
        "controller": "search",
        "orderby": "position",
        "orderway": "desc",
        "search_query": isbn,
        "submit_search": ""
    }

    print(
        "SEARCHING ISBN:",
        isbn,
        flush=True
    )

    response = request_retry(
        session,
        "GET",
        search_url,
        params=params
    )

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    links = get_product_links(
        soup,
        response.url,
        isbn
    )

    print(
        "PRODUCT LINKS:",
        len(links),
        flush=True
    )

    if not links:
        return None

    for index, link in enumerate(links, 1):

        try:

            print(
                f"Checking product {index}/{len(links)}",
                flush=True
            )

            page = request_retry(
                session,
                "GET",
                link
            )

            product = BeautifulSoup(
                page.text,
                "html.parser"
            )

            if not page_has_isbn(
                product,
                page.url,
                isbn
            ):
                continue

            title = get_product_title(product)

            old_price, new_price, discount = extract_prices(
                product
            )

            return {
                "title": title,
                "old": old_price,
                "new": new_price,
                "discount": discount,
                "link": page.url
            }

        except requests.exceptions.RequestException:
            raise

        except Exception:
            log_error(
                "PRODUCT PAGE ERROR:\n" +
                traceback.format_exc()
            )

    return None


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
# 11. SEARCH WORKERS
# =========================================

def search_worker(worker_number):

    tg_local.session = requests.Session()

    tg_local.session.headers.update({
        "User-Agent": "KetabYabBot/5.0"
    })

    shop_local.session = requests.Session()

    shop_local.session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"
    })

    log_error(
        "SEARCH WORKER " +
        str(worker_number) +
        " STARTED"
    )

    while True:

        job = None

        try:

            job = claim_job()

            if job is None:
                time.sleep(JOB_POLL_SECONDS)
                continue

            job_id, chat_id, isbn, attempts = job

            log_error(
                "WORKER " + str(worker_number) +
                " START JOB " + str(job_id) +
                ": " + isbn
            )

            answer = search_mehromah(isbn)

            # تا ارسال موفق نتیجه، کار حذف نمی‌شود.
            send(chat_id, answer)

            finish_job(job_id)

            log_error(
                "JOB " + str(job_id) + " COMPLETED"
            )

        except Exception as error:

            log_error(
                "WORKER " + str(worker_number) +
                " ERROR:\n" +
                traceback.format_exc()
            )

            if job is not None:

                try:
                    retry_job(
                        job[0],
                        job[3],
                        repr(error)
                    )

                except Exception:
                    log_error(
                        "FAILED TO SAVE RETRY:\n" +
                        traceback.format_exc()
                    )
                    time.sleep(5)

            else:
                time.sleep(5)


def start_search_workers():

    for number in range(1, WORKER_COUNT + 1):

        thread = threading.Thread(
            target=search_worker,
            args=(number,),
            daemon=True,
            name=f"SearchWorker-{number}"
        )

        thread.start()


# =========================================
# 12. MENU AND MESSAGE HANDLER
# =========================================

def publisher_menu():

    message = "📚 انتخاب ناشر\n\n"

    for number, name in PUBLISHERS.items():
        message += f"{number} - {name}\n"

    message += "\nعدد ناشر موردنظر را ارسال کن."

    return message


def handle_message(chat_id, raw_text):

    text = digits(raw_text.strip())
    user_id = str(chat_id)

    if text == "/log":

        if not os.path.exists(LOG_FILE):
            send(chat_id, "فایل گزارش هنوز ساخته نشده است.")
            return

        try:

            with open(LOG_FILE, "r", encoding="utf-8") as f:
                content = f.read()

            send(
                chat_id,
                "📋 گزارش ربات:\n\n" +
                (
                    content[-3000:]
                    if content
                    else "گزارش خالی است."
                )
            )

        except Exception:
            send(chat_id, "خواندن گزارش با خطا مواجه شد.")

        return

    if text == "/status":

        selected = user_publishers.get(user_id)

        if selected is None:
            name = "انتخاب نشده"
        else:
            name = PUBLISHERS.get(
                int(selected),
                "نامشخص"
            )

        send(
            chat_id,
            "✅ ربات فعال است.\n\n"
            f"ناشر انتخاب‌شده: {name}\n"
            f"تعداد کارهای در صف: {pending_jobs_count()}\n"
            f"تعداد کارگرها: {WORKER_COUNT}"
        )

        return

    if text in (
        "/start",
        "شروع",
        "ناشرها",
        "منو"
    ):

        selected = user_publishers.get(user_id)

        if selected is None:
            send(chat_id, publisher_menu())

        else:
            selected = int(selected)

            send(
                chat_id,
                f"ناشر فعلی: {PUBLISHERS[selected]}\n\n"
                "برای جستجو، بارکد کتاب را بفرست.\n"
                "برای تغییر ناشر، عدد دیگری از ۱ تا ۲۰ بفرست."
            )

        return

    if text.isdigit() and 1 <= int(text) <= 20:

        number = int(text)

        user_publishers[user_id] = number

        save_state()

        name = PUBLISHERS[number]

        if number == 1:

            send(
                chat_id,
                f"✅ ناشر انتخاب شد: {name}\n\n"
                "حالا بارکد کتاب‌ها را ارسال کن."
            )

        else:

            send(
                chat_id,
                f"✅ ناشر انتخاب شد: {name}\n\n"
                "⏳ جستجوی این ناشر هنوز فعال نشده است.\n"
                "فعلاً مهر و ماه فعال است."
            )

        return

    isbn = normalize_isbn(text)

    if len(isbn) not in (10, 13):

        send(
            chat_id,
            "⚠️ بارکد معتبر نیست.\n\n"
            "لطفاً ISBN ده یا سیزده رقمی را ارسال کن."
        )

        return

    if not valid_isbn(isbn):

        send(
            chat_id,
            "⚠️ رقم کنترل بارکد معتبر نیست.\n"
            "لطفاً دوباره اسکن کن."
        )

        return

    publisher = user_publishers.get(user_id)

    if publisher is None:

        send(
            chat_id,
            "⚠️ ابتدا ناشر را انتخاب کن.\n\n" +
            publisher_menu()
        )

        return

    publisher = int(publisher)

    if publisher != 1:

        send(
            chat_id,
            f"⏳ جستجوی {PUBLISHERS[publisher]} هنوز فعال نشده است."
        )

        return

    try:

        enqueue_job(chat_id, isbn)

        send(
            chat_id,
            "📥 بارکد در صف دائمی ذخیره شد.\n"
            "اگر اینترنت قطع شود، پس از وصل شدن دوباره تلاش می‌کنم."
        )

    except Exception:

        log_error(
            "ENQUEUE ERROR:\n" +
            traceback.format_exc()
        )

        send(
            chat_id,
            "⚠️ ذخیره بارکد ناموفق بود؛ لطفاً دوباره ارسال کن."
        )


# =========================================
# 13. UPDATE PROCESSING
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
                            "MESSAGE ERROR:\n" +
                            traceback.format_exc()
                        )

                        try:
                            send(
                                chat_id,
                                "⚠️ خطای داخلی رخ داد. ربات همچنان فعال است."
                            )

                        except Exception:
                            log_error("ERROR MESSAGE FAILED")

        except Exception:

            log_error(
                "UPDATE ERROR:\n" +
                traceback.format_exc()
            )

        finally:

            offset = update_id + 1
            save_state()


# =========================================
# 14. MAIN
# =========================================

def main():

    global offset

    if not TOKEN.strip():
        raise ValueError(
            "توکن ربات را در قسمت TOKEN وارد کن."
        )

    load_state()

    initialize_job_db()

    start_search_workers()

    try:
        prepare_polling()

    except Exception:
        log_error(
            "WEBHOOK SETUP ERROR:\n" +
            traceback.format_exc()
        )

    print("=" * 40, flush=True)
    print("KETABYAB BOT V5 STARTED", flush=True)
    print("WORKERS:", WORKER_COUNT, flush=True)
    print("PERSISTENT QUEUE:", DB_FILE, flush=True)
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
# 15. RUN
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
            "FATAL ERROR. Check log file.",
            flush=True
        )
