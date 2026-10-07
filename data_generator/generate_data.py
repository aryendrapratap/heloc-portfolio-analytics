"""
Creates realistic, fake HELOC portfolio data and loads it into heloc_db.

What it creates (the same data every run, because the random seed is fixed):
  - 300 customers, each with one home and one HELOC account
  - 24 months of history: October 2024 to September 2026
  - every draw, payment, interest charge and late fee
  - one month-end snapshot per account per month
  - credit limit changes (increases, cuts after late payments, closures)

How the story works:
  - About half the accounts were opened before October 2024 and already
    have a balance when the data starts; the rest open during the 24 months.
  - Each customer has a habit: LOW users borrow little, REVOLVERS keep a
    medium balance, HEAVY users stay near their limit.
  - A small group falls behind on payments. Some catch up, some stay
    on-and-off late, and some keep missing payments until the account
    is charged off at 180 days late.
  - Lower credit scores and heavier borrowing make falling behind more
    likely, so the analysis has real patterns to find.
  - Late payments are set higher than real Canadian levels on purpose, so
    the patterns are visible in a portfolio of only 300 accounts.

Run it after building the database:
    python scripts/build_db.py
    python data_generator/generate_data.py

It empties the customer and account tables first, so it is safe to re-run.
All names, addresses, emails and phone numbers are fake.
"""

import calendar
import os
import random
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from faker import Faker

# -------------------------------------------------------------
# Settings
# -------------------------------------------------------------
SEED = 42
N_CUSTOMERS = 300
WINDOW_START = date(2024, 10, 1)   # first snapshot month
WINDOW_END = date(2026, 9, 1)      # last snapshot month
PAYMENT_DUE_DAY = 25               # minimum payment is due on the 25th
LATE_FEE = Decimal("45.00")
FREEZE_DPD = 60                    # no more borrowing at 60+ days late
CHARGE_OFF_DPD = 180               # written off at 180+ days late

ROOT = Path(__file__).resolve().parent.parent

# code: (share of customers %, average home price, yearly price change,
#        first letters of postal codes, phone area codes, cities)
PROVINCES = {
    "ON": (39.0, 870_000, -0.02, "KLMNP", ["416", "647", "905", "613", "519", "705", "807"],
           ["Toronto", "Ottawa", "Mississauga", "Hamilton", "London", "Kitchener",
            "Sudbury", "Thunder Bay", "Barrie", "Kingston"]),
    "QC": (22.0, 520_000, 0.05, "GHJ", ["514", "438", "418", "819", "450"],
           ["Montreal", "Quebec City", "Laval", "Gatineau", "Sherbrooke", "Trois-Rivieres"]),
    "BC": (13.5, 980_000, -0.01, "V", ["604", "778", "250", "236"],
           ["Vancouver", "Surrey", "Burnaby", "Victoria", "Kelowna", "Kamloops", "Nanaimo"]),
    "AB": (12.0, 500_000, 0.04, "T", ["403", "587", "780", "825"],
           ["Calgary", "Edmonton", "Red Deer", "Lethbridge", "Airdrie"]),
    "MB": (3.5, 380_000, 0.03, "R", ["204", "431"], ["Winnipeg", "Brandon", "Steinbach"]),
    "SK": (3.0, 320_000, 0.03, "S", ["306", "639"], ["Saskatoon", "Regina", "Prince Albert"]),
    "NS": (2.7, 450_000, 0.04, "B", ["902", "782"], ["Halifax", "Dartmouth", "Sydney", "Truro"]),
    "NB": (2.1, 330_000, 0.04, "E", ["506"], ["Moncton", "Saint John", "Fredericton"]),
    "NL": (1.3, 330_000, 0.02, "A", ["709"], ["St. John's", "Mount Pearl", "Corner Brook"]),
    "PE": (0.4, 400_000, 0.04, "C", ["902", "782"], ["Charlottetown", "Summerside"]),
    "NT": (0.15, 450_000, 0.01, "X", ["867"], ["Yellowknife"]),
    "YT": (0.15, 600_000, 0.02, "Y", ["867"], ["Whitehorse"]),
    "NU": (0.1, 550_000, 0.01, "X", ["867"], ["Iqaluit"]),
}
POSTAL_LETTERS = "ABCEGHJKLMNPRSTVWXYZ"

# type: (share %, price factor)
PROPERTY_TYPES = {
    "DETACHED": (55, 1.10),
    "SEMI_DETACHED": (10, 0.85),
    "TOWNHOUSE": (15, 0.80),
    "CONDO": (20, 0.60),
}

# habit: (share %, target utilization range, chance of borrowing in a month)
PROFILES = {
    "LOW": (35, (0.00, 0.15), 0.20),
    "REVOLVER": (45, (0.25, 0.65), 0.55),
    "HEAVY": (20, (0.70, 0.95), 0.60),
}

CENT = Decimal("0.01")


# -------------------------------------------------------------
# Small helpers
# -------------------------------------------------------------
def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def rate4(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def round_down(value: float, step: int) -> int:
    return int(value // step * step)


def round_up(value: float, step: int) -> int:
    return int(-(-value // step) * step)


def add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def random_date(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, (end - start).days))


def day_in_month(month: date, first_day: int, last_day: int) -> date:
    last_day = min(last_day, month_end(month).day)
    first_day = min(first_day, last_day)
    return date(month.year, month.month, random.randint(first_day, last_day))


def ascii_only(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in text if ch.isalnum()).lower()


def weighted_choice(options: dict) -> str:
    keys = list(options)
    weights = [options[k][0] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def postal_code(province: str) -> str:
    first = random.choice(PROVINCES[province][3])
    letter = lambda: random.choice(POSTAL_LETTERS)  # noqa: E731
    digit = lambda: str(random.randint(0, 9))       # noqa: E731
    return f"{first}{digit()}{letter()} {digit()}{letter()}{digit()}"


def dpd_bucket(dpd: int) -> str:
    if dpd == 0:
        return "CURRENT"
    if dpd < 30:
        return "DPD_1_29"
    if dpd < 60:
        return "DPD_30_59"
    if dpd < 90:
        return "DPD_60_89"
    if dpd < CHARGE_OFF_DPD:
        return "DPD_90_179"
    return "CHARGED_OFF"


# -------------------------------------------------------------
# Database
# -------------------------------------------------------------
def connect():
    load_dotenv(ROOT / ".env")
    return pymysql.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        autocommit=False,
    )


def load_prime_rates(cur) -> list[tuple[date, Decimal]]:
    cur.execute("SELECT effective_date, prime_rate FROM prime_rate_history ORDER BY effective_date")
    rows = cur.fetchall()
    if not rows:
        raise SystemExit("prime_rate_history is empty. Run: python scripts/build_db.py")
    return rows


def prime_on(prime_rates, day: date) -> Decimal:
    current = prime_rates[0][1]
    for effective_date, rate in prime_rates:
        if effective_date <= day:
            current = rate
        else:
            break
    return current


# -------------------------------------------------------------
# One account's story
# -------------------------------------------------------------
@dataclass
class Account:
    account_id: int
    customer_id: int
    property_id: int
    open_date: date
    limit: int
    max_limit: int
    margin: Decimal
    score: int
    profile: str
    target_util: float
    appraised_value: float
    mortgage: float
    price_growth: float
    appraisal_date: date
    distressed: bool = False
    distress_path: str = ""
    distress_start: int = 0
    cure_after: int = 0
    misses: int = 0
    consecutive_misses: int = 0
    cured: bool = False
    ever_late: bool = False
    increased: bool = False
    status: str = "ACTIVE"
    close_date: date | None = None
    balance: Decimal = Decimal("0.00")
    unpaid: list = field(default_factory=list)   # [due_date, amount] oldest first
    last_interest: Decimal = Decimal("0.00")
    last_fees: Decimal = Decimal("0.00")
    last_activity: date | None = None


def simulate(acct: Account, prime_rates, transactions, snapshots, limit_changes) -> None:
    first_month = max(WINDOW_START, acct.open_date.replace(day=1))
    months = [add_months(first_month, i) for i in range(months_between(first_month, WINDOW_END) + 1)]
    low, high = PROFILES[acct.profile][1]
    draw_chance = PROFILES[acct.profile][2]

    # Who falls behind, and how their story ends
    if acct.distressed and len(months) >= 5:
        acct.distress_start = random.randint(2, len(months) - 1)
        acct.distress_path = random.choices(["CURE", "ROLL", "CHRONIC"], weights=[40, 35, 25])[0]
        acct.cure_after = random.randint(1, 3)
    else:
        acct.distressed = False

    # Accounts opened before Oct 2024 already have a balance when the data starts
    if acct.open_date < WINDOW_START:
        if acct.profile == "LOW" and random.random() < 0.4:
            start = 0.0
        else:
            start = acct.limit * acct.target_util * random.uniform(0.8, 1.1)
        acct.balance = money(min(start, acct.limit))
        rate = prime_on(prime_rates, WINDOW_START - timedelta(days=1)) + acct.margin
        acct.last_interest = money(acct.balance * rate / 12)
        acct.score = max(300, min(900, acct.score + int(random.gauss(0, 20))))

    def add_txn(day, kind, amount, channel, description):
        transactions.append((acct.account_id, day, kind, amount, channel, description,
                             datetime.combine(day, time(random.randint(8, 20), random.randint(0, 59)))))
        acct.last_activity = day

    for i, month in enumerate(months):
        end = month_end(month)
        opening = acct.balance
        draws = payments = interest = fees = Decimal("0.00")
        start_day = acct.open_date.day if month == acct.open_date.replace(day=1) else 1

        # This month's minimum payment: last month's interest and fees, plus anything still unpaid
        new_due = acct.last_interest + acct.last_fees
        if new_due > 0:
            acct.unpaid.append([date(month.year, month.month, PAYMENT_DUE_DAY), new_due])
        total_due = sum((amount for _, amount in acct.unpaid), Decimal("0.00"))

        # Is this customer behind on payments this month?
        in_distress = acct.distressed and i >= acct.distress_start and not acct.cured
        miss = False
        if in_distress and total_due > 0:
            if acct.distress_path == "ROLL":
                miss = True
            elif acct.distress_path == "CURE":
                miss = acct.misses < acct.cure_after
                if not miss:
                    acct.cured = True
            else:  # CHRONIC: on-and-off late, usually catches up before 60 days
                miss = acct.consecutive_misses < 2 and random.random() < 0.55

        # Will they close the account this month? (pay everything off)
        closing_now = (
            not acct.distressed and acct.status == "ACTIVE" and acct.profile != "HEAVY"
            and i >= 6 and not acct.unpaid[:-1] and random.random() < 0.003
        )

        # --- Borrowing (draws) ---
        if acct.status == "ACTIVE" and not in_distress and not closing_now:
            acct.target_util = min(high, max(low, acct.target_util + random.gauss(0, 0.03)))
            target = acct.target_util
            chance = draw_chance
            if acct.distressed and acct.distress_start - 2 <= i < acct.distress_start:
                target, chance = 0.95, 0.9        # borrows heavily just before falling behind
            if acct.open_date.replace(day=1) == month:
                chance = 0.85                     # most new accounts borrow right away

            gap = acct.limit * target - float(acct.balance)
            amount = 0
            if gap > 500 and random.random() < chance:
                amount = gap * random.uniform(0.3, 1.0)
            elif acct.profile == "LOW" and random.random() < 0.35:
                amount = random.uniform(500, 5000)
            amount = min(round_down(amount, 100), round_down(acct.limit - float(acct.balance), 100))
            if amount >= 500:
                parts = 1 if amount < 2000 else random.choice([1, 2, 2, 3])
                piece = round_down(amount / parts, 100)
                for p in range(parts):
                    value = money(amount - piece * (parts - 1) if p == parts - 1 else piece)
                    day = day_in_month(month, start_day, max(19, start_day))
                    channel = "ONLINE" if random.random() < 0.8 else "BRANCH"
                    add_txn(day, "DRAW", value, channel, "Funds withdrawal")
                    draws += value
                    acct.balance += value

        # --- Minimum payment ---
        if total_due > 0 and not miss:
            pay = min(total_due, acct.balance)
            if pay > 0:
                caught_up = len(acct.unpaid) > 1
                day = day_in_month(month, 20, PAYMENT_DUE_DAY)
                if caught_up:
                    add_txn(day, "PAYMENT", pay, random.choice(["ONLINE", "BRANCH"]),
                            "Payment to bring account current")
                else:
                    channel = "AUTO_DEBIT" if random.random() < 0.8 else "ONLINE"
                    add_txn(day, "PAYMENT", pay, channel, "Minimum payment")
                payments += pay
                acct.balance -= pay
            acct.unpaid = []
            acct.consecutive_misses = 0
        elif miss:
            acct.misses += 1
            acct.consecutive_misses += 1
            acct.ever_late = True

        # --- Extra payments toward what they borrowed ---
        if acct.balance > 0 and not in_distress:
            extra = Decimal("0.00")
            if closing_now:
                extra = acct.balance
            elif acct.profile == "LOW":
                if random.random() < 0.5:
                    extra = acct.balance
                elif random.random() < 0.5:
                    extra = money(float(acct.balance) * random.uniform(0.1, 0.4))
            elif acct.profile == "REVOLVER" and random.random() < 0.75:
                extra = money(max(100, round(float(acct.balance) * random.uniform(0.01, 0.04), -1)))
            elif acct.profile == "HEAVY" and random.random() < 0.45:
                extra = money(max(100, round(float(acct.balance) * random.uniform(0.005, 0.015), -1)))
            extra = min(extra, acct.balance)
            if extra > 0:
                description = "Final payment - account closed" if closing_now else "Additional principal payment"
                add_txn(day_in_month(month, max(26, start_day), max(28, start_day)), "PAYMENT", extra, "ONLINE", description)
                payments += extra
                acct.balance -= extra

        # --- Interest and late fee ---
        rate = prime_on(prime_rates, end) + acct.margin
        if acct.balance > 0:
            interest = money(acct.balance * rate / 12)
            if interest > 0:
                add_txn(end, "INTEREST", interest, "SYSTEM", "Monthly interest")
        if miss:
            fees = LATE_FEE
            add_txn(day_in_month(month, 26, 26), "FEE", fees, "SYSTEM", "Late payment fee")
        acct.balance += interest + fees
        acct.last_interest, acct.last_fees = interest, fees

        # --- How late are they at month-end? ---
        dpd = (end - acct.unpaid[0][0]).days if acct.unpaid else 0
        bucket = dpd_bucket(dpd)

        # --- Credit score moves ---
        if miss:
            acct.score -= random.randint(25, 55)
        elif acct.ever_late and dpd == 0:
            acct.score += random.randint(2, 8)
        else:
            acct.score += round(random.gauss(0.3, 4))
        if acct.limit and float(acct.balance) / acct.limit > 0.9:
            acct.score -= 2
        acct.score = max(300, min(900, acct.score))

        # --- Home value and mortgage this month ---
        years = (end - acct.appraisal_date).days / 365.25
        value = acct.appraised_value * (1 + acct.price_growth) ** years
        mortgage = acct.mortgage * max(0.0, 1 - 0.0035 * months_between(acct.appraisal_date, month))

        # --- Month-end account changes ---
        snapshot_limit = acct.limit
        if dpd >= CHARGE_OFF_DPD:
            acct.status, acct.close_date = "CHARGED_OFF", end
            limit_changes.append((acct.account_id, end, acct.limit, 0, "ACCOUNT_CLOSURE"))
            acct.limit = 0
        elif closing_now and acct.balance == 0:
            closed_on = acct.last_activity if acct.last_activity and acct.last_activity >= month else end
            acct.status, acct.close_date = "CLOSED", closed_on
            limit_changes.append((acct.account_id, acct.close_date, acct.limit, 0, "ACCOUNT_CLOSURE"))
            acct.limit = 0
        else:
            if dpd >= FREEZE_DPD and acct.status == "ACTIVE":
                acct.status = "FROZEN"
            elif dpd == 0 and acct.status == "FROZEN":
                acct.status = "ACTIVE"
                new_limit = max(round_up(float(acct.balance), 5000), round_down(acct.limit * 0.75, 5000))
                if new_limit < acct.limit:
                    limit_changes.append((acct.account_id, end, acct.limit, new_limit, "RISK_REDUCTION"))
                    acct.limit = new_limit
            # Home value dropped too far: bring limit back within the 80% rule
            cap = round_down(0.80 * value - mortgage, 5000)
            if (mortgage + acct.limit) / value > 0.85:
                new_limit = max(round_up(float(acct.balance), 5000), cap, 0)
                if new_limit < acct.limit:
                    limit_changes.append((acct.account_id, end, acct.limit, new_limit, "PROPERTY_REVALUATION"))
                    acct.limit = new_limit
            # Good customers using most of their limit sometimes ask for more
            if (acct.status == "ACTIVE" and not acct.ever_late and not acct.increased
                    and acct.score >= 700 and acct.limit
                    and float(acct.balance) / acct.limit >= 0.6 and random.random() < 0.04):
                new_limit = min(acct.max_limit, round_down(acct.limit * random.uniform(1.2, 1.5), 5000))
                if new_limit > acct.limit:
                    limit_changes.append((acct.account_id, end, acct.limit, new_limit, "INCREASE_REQUEST"))
                    acct.limit = new_limit
                    acct.increased = True
            snapshot_limit = acct.limit

        utilization = rate4(float(acct.balance) / snapshot_limit) if snapshot_limit else Decimal("0.0000")
        cltv = rate4((mortgage + snapshot_limit) / value)
        snapshots.append((
            acct.account_id, month, opening, draws, payments, interest, fees, acct.balance,
            money(snapshot_limit), utilization, prime_on(prime_rates, end), rate,
            total_due, dpd, bucket, acct.score, money(value), cltv,
        ))

        if acct.status in ("CHARGED_OFF", "CLOSED"):
            break


# -------------------------------------------------------------
# Build customers, homes and accounts
# -------------------------------------------------------------
def build_portfolio(prime_rates):
    fake = Faker("en_CA")
    Faker.seed(SEED)
    random.seed(SEED)

    customers, properties, accounts = [], [], []
    transactions, snapshots, limit_changes = [], [], []
    used_emails = set()

    for n in range(1, N_CUSTOMERS + 1):
        province = weighted_choice(PROVINCES)
        _, avg_price, growth, _, area_codes, cities = PROVINCES[province]

        # Customer
        first, last = fake.first_name(), fake.last_name()
        age = random.randint(27, 76)
        dob = date(2024 - age, random.randint(1, 12), random.randint(1, 28))
        if age >= 65:
            employment = random.choices(["RETIRED", "EMPLOYED", "SELF_EMPLOYED"], weights=[70, 20, 10])[0]
        else:
            employment = random.choices(["EMPLOYED", "SELF_EMPLOYED", "OTHER"], weights=[82, 14, 4])[0]
        income = random.lognormvariate(11.46, 0.45) * (0.6 if employment == "RETIRED" else 1.0)
        income = max(35_000, min(600_000, round(income, -2)))

        email = f"{ascii_only(first)}.{ascii_only(last)}@example.com"
        while email in used_emails:
            email = f"{ascii_only(first)}.{ascii_only(last)}{random.randint(1, 999)}@example.com"
        used_emails.add(email)
        phone = f"{random.choice(area_codes)}-555-{random.randint(0, 9999):04d}"

        if random.random() < 0.55:
            open_date = random_date(date(2019, 1, 1), date(2024, 9, 30))
        else:
            open_date = random_date(WINDOW_START, date(2026, 6, 30))
        created_at = datetime.combine(open_date - timedelta(days=random.randint(5, 60)),
                                      time(random.randint(9, 17), random.randint(0, 59)))
        customers.append((n, first, last, dob, email, phone, province, money(income), employment, created_at))

        # Home and credit limit (Canadian rules: limit <= 65% of value, mortgage + limit <= 80%)
        prop_type = weighted_choice(PROPERTY_TYPES)
        value = avg_price * PROPERTY_TYPES[prop_type][1] * random.lognormvariate(0, 0.3)
        value = max(150_000, min(5_000_000, round(value, -3)))
        for _ in range(20):
            mortgage = 0.0 if random.random() < 0.15 else round(value * random.uniform(0.15, 0.62), -2)
            max_limit = round_down(min(0.65 * value, 0.80 * value - mortgage, 5 * income), 5000)
            limit = round_down(max_limit * random.uniform(0.55, 1.0), 5000)
            if limit >= 15_000:
                break
        else:
            mortgage, max_limit = 0.0, round_down(min(0.65 * value, 5 * income), 5000)
            limit = max_limit
        appraisal_date = open_date - timedelta(days=random.randint(7, 30))
        properties.append((n, n, fake.street_address()[:100], random.choice(cities), province,
                           postal_code(province), prop_type, money(value), appraisal_date, money(mortgage)))

        # Pricing: better credit score = smaller margin over prime
        score = max(620, min(900, int(random.gauss(745, 50))))
        if score >= 760:
            margin = random.choice(["0.0000", "0.0025", "0.0050"])
        elif score >= 700:
            margin = random.choice(["0.0050", "0.0075", "0.0100"])
        else:
            margin = random.choice(["0.0100", "0.0150", "0.0200"])

        profile = weighted_choice(PROFILES)
        low, high = PROFILES[profile][1]

        # Chance of falling behind: higher for low scores, heavy borrowers and high CLTV
        risk = 0.09
        risk *= 2.5 if score < 680 else 1.6 if score < 720 else 0.4 if score >= 780 else 1.0
        risk *= {"LOW": 0.4, "REVOLVER": 1.0, "HEAVY": 1.8}[profile]
        if (mortgage + limit) / value > 0.75:
            risk *= 1.3

        acct = Account(
            account_id=n, customer_id=n, property_id=n, open_date=open_date,
            limit=limit, max_limit=max(limit, max_limit), margin=Decimal(margin), score=score,
            profile=profile, target_util=random.uniform(low, high),
            appraised_value=value, mortgage=mortgage,
            price_growth=growth + random.gauss(0, 0.015), appraisal_date=appraisal_date,
            distressed=random.random() < min(risk, 0.6),
        )
        original_limit, original_score = acct.limit, acct.score
        simulate(acct, prime_rates, transactions, snapshots, limit_changes)

        accounts.append((
            n, f"HL-{n:06d}", n, n, open_date, original_limit, acct.limit, acct.margin,
            acct.balance, original_score, acct.status, acct.close_date,
            datetime.combine(open_date, time(10, 0)),
            datetime.combine(acct.last_activity or open_date, time(23, 59)),
        ))

    transactions.sort(key=lambda t: (t[1], t[0]))
    return customers, properties, accounts, transactions, snapshots, limit_changes


# -------------------------------------------------------------
# Save to MySQL
# -------------------------------------------------------------
def save(conn, customers, properties, accounts, transactions, snapshots, limit_changes) -> None:
    with conn.cursor() as cur:
        # Tells future triggers (Step 5) this is a historical data load
        cur.execute("SET @bulk_load = 1")

        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in ["audit_log", "monthly_snapshot", "credit_limit_change",
                      "account_transaction", "heloc_account", "property", "customer"]:
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        def insert(sql, rows):
            for start in range(0, len(rows), 1000):
                cur.executemany(sql, rows[start:start + 1000])

        insert("""INSERT INTO customer (customer_id, first_name, last_name, date_of_birth, email, phone,
                  province_code, annual_income, employment_status, created_at)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", customers)
        insert("""INSERT INTO property (property_id, customer_id, street_address, city, province_code,
                  postal_code, property_type, appraised_value, appraisal_date, mortgage_balance)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", properties)
        insert("""INSERT INTO heloc_account (account_id, account_number, customer_id, property_id, open_date,
                  original_credit_limit, credit_limit, rate_margin, current_balance,
                  origination_credit_score, account_status, close_date, created_at, updated_at)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", accounts)
        insert("""INSERT INTO account_transaction (account_id, transaction_date, transaction_type, amount,
                  channel, description, created_at)
                  VALUES (%s, %s, %s, %s, %s, %s, %s)""", transactions)
        insert("""INSERT INTO credit_limit_change (account_id, change_date, old_limit, new_limit, reason)
                  VALUES (%s, %s, %s, %s, %s)""", limit_changes)
        insert("""INSERT INTO monthly_snapshot (account_id, snapshot_month, opening_balance, total_draws,
                  total_payments, interest_charged, fees_charged, closing_balance, credit_limit,
                  utilization_rate, prime_rate, interest_rate, minimum_payment_due, days_past_due,
                  bucket_code, credit_score, property_value, cltv)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
               snapshots)
    conn.commit()


def print_summary(conn) -> None:
    with conn.cursor() as cur:
        print("\nRows created:")
        for table in ["customer", "property", "heloc_account", "account_transaction",
                      "credit_limit_change", "monthly_snapshot"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"  {table:<22} {cur.fetchone()[0]:>7,}")

        cur.execute("""
            SELECT COUNT(*),
                   SUM(closing_balance),
                   AVG(utilization_rate),
                   SUM(days_past_due >= 30) / COUNT(*)
            FROM monthly_snapshot
            WHERE snapshot_month = %s AND bucket_code <> 'CHARGED_OFF'
        """, (WINDOW_END,))
        n, balance, util, late = cur.fetchone()
        cur.execute("""SELECT COUNT(*), COALESCE(SUM(current_balance), 0)
                       FROM heloc_account WHERE account_status = 'CHARGED_OFF'""")
        charged_off, written_off = cur.fetchone()

    print(f"\nPortfolio at the end of {WINDOW_END:%B %Y}:")
    print(f"  Open accounts           {n:>12,}")
    print(f"  Total balance           ${balance:>14,.2f}")
    print(f"  Average utilization     {util:>12.1%}")
    print(f"  30+ days late           {late:>12.1%}")
    print(f"  Charged off (2 years)   {charged_off:>12,}  (${written_off:,.2f})")


def main() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            prime_rates = load_prime_rates(cur)
        print("Generating data...")
        data = build_portfolio(prime_rates)
        print("Saving to MySQL...")
        save(conn, *data)
        print_summary(conn)
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
