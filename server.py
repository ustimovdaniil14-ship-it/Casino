# pip install fastapi uvicorn aiogram==3.*
# BOT_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
import os, re, hmac, hashlib, json, time, secrets, asyncio, contextlib
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, F
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

TOKEN = os.environ["BOT_TOKEN"]
RTP, DEP_MAX, WD_LIMIT = 0.9, 100_000, 50_000
def _env(name, default, cast=float): return cast(os.environ.get(name, default))
MAX_BET   = _env("MAX_BET", 5000, int)
WIN_CAP   = _env("WIN_CAP", 50000, int)
COOLDOWN  = _env("COOLDOWN", 1.0)
OWNER_ID  = 1780245250  # ТВОЙ ID
ROLLOVER  = _env("ROLLOVER", 1.0)

def fmtn(n): return f"{int(n):,}".replace(",", " ")
def cap_err(): return HTTPException(400, f"Максимальный выигрыш за раунд {fmtn(WIN_CAP)} ⭐. Уменьши ставку или множитель")
def bet_err(): return HTTPException(400, f"Максимальная ставка {fmtn(MAX_BET)} ⭐")

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
API_BASE = os.environ.get("API_BASE", "https://api.staticgram.top").rstrip("/")
session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE)) if API_BASE else None
bot, dp = Bot(TOKEN, session=session), Dispatcher()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG = DATABASE_URL.startswith("postgres")
if PG:
    import psycopg
    from psycopg.rows import dict_row
    def _connect():
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10, prepare_threshold=None)
else:
    import sqlite3
    def _connect():
        c = sqlite3.connect(os.environ.get("DB_PATH", "casino.db"), check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

class DB:
    def __init__(self): self.c = _connect(); self.in_tx = False
    def execute(self, sql, params=()):
        if PG: sql = sql.replace("?", "%s")
        for attempt in (0, 1):
            try:
                return self.c.execute(sql, params)
            except Exception as e:
                if PG and attempt == 0 and not self.in_tx and isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)):
                    self.c = _connect(); continue
                raise

db = DB()
INT = "BIGINT" if PG else "INTEGER"
SERIAL = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
REAL = "DOUBLE PRECISION" if PG else "REAL"
BIGGER = "GREATEST" if PG else "MAX"

for stmt in (
    f"CREATE TABLE IF NOT EXISTS users(id {INT} PRIMARY KEY, balance {INT} DEFAULT 0, deposited {INT} DEFAULT 0,"
    f" withdrawn {INT} DEFAULT 0, won_total {INT} DEFAULT 0, played {INT} DEFAULT 0, won {INT} DEFAULT 0,"
    f" wagered {INT} DEFAULT 0, best {INT} DEFAULT 0, wd_day TEXT DEFAULT '', wd_used {INT} DEFAULT 0)",
    f"CREATE TABLE IF NOT EXISTS ops(n {SERIAL}, uid {INT}, k TEXT, a {INT}, b {INT}, p {INT}, x {REAL},"
    f" g TEXT, cid TEXT UNIQUE, ts {INT})",
    "CREATE INDEX IF NOT EXISTS ops_uid ON ops(uid, n)",
    f"CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount {INT}, max_uses {INT}, used {INT} DEFAULT 0, active {INT} DEFAULT 1, created {INT})",
    f"CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, uid {INT}, ts {INT}, PRIMARY KEY(code, uid))",
    f"CREATE TABLE IF NOT EXISTS hub_users(id {INT} PRIMARY KEY, first_seen {INT}, last_seen {INT})",
    f"CREATE TABLE IF NOT EXISTS inv(id TEXT PRIMARY KEY, uid {INT}, case_id TEXT, name TEXT, value {INT}, ts {INT}, sold {INT} DEFAULT 0)",
):
    db.execute(stmt)

try: db.execute(f"ALTER TABLE users ADD COLUMN bonus {INT} DEFAULT 0")
except Exception: pass
try: db.execute(f"ALTER TABLE users ADD COLUMN daily_ts {INT} DEFAULT 0")
except Exception: pass

@contextlib.contextmanager
def tx():
    db.execute("BEGIN"); db.in_tx = True
    try: yield
    except BaseException:
        try: db.execute("ROLLBACK")
        except Exception: db.c = _connect()
        raise
    else: db.execute("COMMIT")
    finally: db.in_tx = False

lock = asyncio.Lock()
seen = {}
def online():
    now = time.time()
    for k in [k for k, v in seen.items() if now - v > 300]: del seen[k]
    return sum(1 for v in seen.values() if now - v <= 45)

def auth(request: Request) -> int:
    raw = request.headers.get("X-Init-Data", "")
    d = dict(parse_qsl(raw, keep_blank_values=True)); h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "bad initData")
    if time.time() - int(d.get("auth_date", 0)) > 86400: raise HTTPException(401, "expired")
    uid = json.loads(d["user"])["id"]
    seen[uid] = time.time()
    db.execute("INSERT INTO users(id) VALUES(?) ON CONFLICT DO NOTHING", (uid,))
    
    now = int(time.time())
    if db.execute("SELECT 1 FROM hub_users WHERE id=?", (uid,)).fetchone():
        db.execute("UPDATE hub_users SET last_seen=? WHERE id=?", (now, uid))
    else:
        db.execute("INSERT INTO hub_users(id, first_seen, last_seen) VALUES(?,?,?)", (uid, now, now))
        
    return uid

last_bet = {}
def gate(uid):
    now = time.monotonic()
    if now - last_bet.get(uid, 0) < COOLDOWN: raise HTTPException(429, "Не так быстро, подожди секунду")
    last_bet[uid] = now
    if len(last_bet) > 5000:
        for k in [k for k, v in last_bet.items() if now - v > 60]: del last_bet[k]

def state(uid):
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    ops = [dict(k=r["k"], a=r["a"], b=r["b"], p=r["p"], x=r["x"], g=r["g"], id=r["cid"], ts=r["ts"])
           for r in db.execute("SELECT * FROM ops WHERE uid=? ORDER BY n DESC LIMIT 100", (uid,))]
    today = time.strftime("%Y-%m-%d")
    
    daily_ts = u["daily_ts"] if u and "daily_ts" in u.keys() else 0
    daily_left = max(0, 86400 - (int(time.time()) - (daily_ts or 0)))
    
    return dict(balance=u["balance"], deposited=u["deposited"], withdrawn=u["withdrawn"], wonTotal=u["won_total"],
                played=u["played"], won=u["won"], wagered=u["wagered"], best=u["best"], bonus=u["bonus"] or 0, ops=ops,
                wd=dict(d=u["wd_day"] or today, u=u["wd_used"] if u["wd_day"] == today else 0), daily_left=daily_left)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

@app.exception_handler(Exception)
async def _any(_, e):
    print("ERROR:", repr(e), flush=True)
    return JSONResponse({"error": f"{type(e).__name__}: {e}"[:200]}, 500)

@app.exception_handler(HTTPException)
async def _h(_, e): return JSONResponse({"error": e.detail}, e.status_code)

@app.api_route("/health", methods=["GET", "HEAD"])
async def health(): return {"ok": True}

@app.api_route("/", methods=["GET", "HEAD"])
async def index(): return FileResponse("index.html")

@app.get("/api/state")
async def get_state(request: Request):
    uid = auth(request)
    return {"state": state(uid), "online": online()}

@app.get("/api/stats")
async def get_stats(request: Request):
    uid = auth(request)
    if OWNER_ID and uid != OWNER_ID: raise HTTPException(403, "Нет доступа")
    now = int(time.time())
    total = db.execute("SELECT COUNT(*) c FROM hub_users").fetchone()["c"]
    today_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    today = db.execute("SELECT COUNT(*) c FROM hub_users WHERE first_seen>=?", (today_start,)).fetchone()["c"]
    return {"total": total, "online": online(), "today": today}

# --- КЕЙСЫ ---
ITEMS = {
    "bear": ("Мишка", 15, "🧸"), "heart": ("Сердце", 15, "💝"), "gift": ("Подарок", 25, "🎁"), "rose": ("Роза", 25, "🌹"),
    "cake": ("Торт", 50, "🎂"), "bouq": ("Букет", 50, "💐"), "rocket": ("Ракета", 50, "🚀"), "champ": ("Шампанское", 50, "🍾"),
    "cup": ("Кубок", 100, "🏆"), "ring": ("Кольцо", 100, "💍"), "dia": ("Алмаз", 100, "💎"), "sbear": ("Static-мишка", 100, "🐻"),
    "lolly": ("Леденец", 800, "🍭"), "box": ("Подарок 2025", 1000, "📦"), "cane": ("Леденец-трость", 1200, "🍬"),
    "eskimo": ("Эскимо", 2000, "🍦"), "fist": ("Золотой кулак", 2888, "💪"), "tile": ("Сердце-плитка", 2888, "❤️"),
    "book": ("Книга", 3000, "📕"), "ufc": ("UFC-бокс", 3500, "🥊"), "glass": ("Роза в колбе", 3500, "🥀"),
    "statue": ("Статуя Свободы", 3700, "🗽"), "lamp": ("Фонарь", 4000, "🏮"), "crystal": ("Кристалл", 4444, "🔮"),
    "voodoo": ("Вуду-кукла", 5000, "🪆"), "cal": ("Календарь", 5500, "📅"), "watch": ("Часы", 5555, "⌚"),
    "hat": ("Шапка", 6000, "🎩"), "bell": ("Колокольчики", 8000, "🔔"), "gring": ("Золотое кольцо", 8000, "💍"),
    "helmet": ("Шлем-кот", 8500, "🐱"), "car": ("Снуп Догг", 8998, "🚗"), "cap": ("Кепка", 9500, "🧢"),
    "cat15": ("Рыжий кот", 15000, "🐈"), "bear17": ("Чёрный мишка", 17000, "🐻"), "cat25": ("Чёрный кот", 25000, "🐈‍⬛"),
    "heartc": ("Космическое сердце", 40000, "💖"),
}
CASES = [
    ("start", "Стартовый", 50, "bear heart gift rose cake bouq rocket champ cup ring dia sbear"),
    ("silver", "Серебряный", 150, "cake bouq rocket champ cup ring dia sbear lolly"),
    ("gold", "Золотой", 500, "cup ring dia sbear lolly box cane eskimo"),
    ("diamond", "Алмазный", 2000, "lolly box cane eskimo fist tile book ufc glass"),
    ("legend", "Легенда", 5000, "eskimo fist tile book ufc glass statue lamp crystal voodoo watch cal hat bell gring helmet car cap"),
    ("platinum", "Платина", 8000, "crystal voodoo cal watch hat bell gring helmet car cap cat15"),
    ("emperor", "Император", 12000, "hat bell gring helmet car cap cat15 bear17 cat25"),
]
RTP_TARGET = 0.86
def _build(cid, title, price, keys):
    vals = [ITEMS[k][1] for k in keys]
    lo, hi = -1.0, 8.0
    for _ in range(80):
        k = (lo + hi) / 2; ws = [v ** -k for v in vals]
        if sum(w * v for w, v in zip(ws, vals)) / sum(ws) / price > RTP_TARGET: lo = k
        else: hi = k
    ws = [v ** -k for v in vals]; tot = sum(ws)
    prizes = [dict(k=x, name=ITEMS[x][0], value=ITEMS[x][1], ic=ITEMS[x][2], w=max(1, round(w / tot * 100000))) for x, w in zip(keys, ws)]
    return dict(id=cid, name=title, price=price, prizes=prizes)

@app.get("/api/cases")
async def cases_catalog(request: Request):
    uid = auth(request)
    out = []
    for cid, title, price, keys in CASES:
        c = _build(cid, title, price, keys)
        tot = sum(p["w"] for p in c["prizes"])
        out.append(dict(id=c["id"], name=c["name"], price=c["price"], rtp=0.86,
                        prizes=[dict(k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], p=p["w"]/tot) for p in c["prizes"]]))
    return {"gifts": [dict(k=k, name=v[0], value=v[1], ic=v[2]) for k, v in sorted(ITEMS.items(), key=lambda kv: kv[1][1])], "cases": out}

# --- ОСТАЛЬНОЕ ---
@app.post("/api/deposit")
async def deposit(request: Request):
    uid = auth(request); a = int((await request.json()).get("amount", 0))
    if not 1 <= a <= DEP_MAX: raise HTTPException(400, "Неверная сумма")
    link = await bot.create_invoice_link(title="Пополнение", description=f"{a} звёзд", payload=f"dep:{uid}:{a}",
        currency="XTR", prices=[LabeledPrice(label="Пополнение", amount=a)])
    return {"link": link}

@app.post("/api/deposit_chat")
async def deposit_chat(request: Request):
    uid = auth(request); a = int((await request.json()).get("amount", 0))
    if not 1 <= a <= DEP_MAX: raise HTTPException(400, "Неверная сумма")
    await bot.send_invoice(chat_id=uid, title="Пополнение", description=f"{a} звёзд", payload=f"dep:{uid}:{a}",
        currency="XTR", prices=[LabeledPrice(label="Пополнение", amount=a)])
    return {"ok": True}

async def sg_call(method, **params):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{API_BASE}/bot{TOKEN}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json(content_type=None)

WD_ERRORS = {
    "BALANCE_TOO_LOW": "В кассе сейчас не хватает звёзд. Попробуй позже",
    "STAR_COUNT_TOO_SMALL": "Слишком маленькая сумма вывода",
    "STAR_WITHDRAWAL_DISABLED": "Вывод временно отключён",
    "Too Many Requests": "Слишком часто. Подожди минуту и повтори",
}

@app.post("/api/withdraw")
async def withdraw(request: Request):
    uid = auth(request); a = int((await request.json()).get("amount", 0))
    cid = "wd" + secrets.token_hex(12); ts = int(time.time() * 1000)
    async with lock:
        s = state(uid)
        if a < 1 or a > s["balance"]: raise HTTPException(400, "Не хватает звёзд")
        if a > WD_LIMIT - s["wd"]["u"]: raise HTTPException(400, "Дневной лимит")
        need = int((s["deposited"] + s["bonus"]) * ROLLOVER - s["wagered"] + .999)
        if need > 0: raise HTTPException(400, f"Чтобы вывести, сыграй ещё на {fmtn(need)} ⭐ ставок")
        with tx():
            db.execute("UPDATE users SET balance=balance-?, withdrawn=withdrawn+?, wd_day=?, wd_used=? WHERE id=?",
                       (a, a, s["wd"]["d"], s["wd"]["u"] + a, uid))
            db.execute("INSERT INTO ops(uid,k,a,cid,ts) VALUES(?,?,?,?,?)", (uid, "wd", a, cid, ts))
    res = None
    for _ in range(2):
        try:
            res = await sg_call("withdrawStarBalance", user_id=uid, star_count=a, request_id=cid)
            break
        except Exception as e:
            print("WITHDRAW NET ERROR:", repr(e), flush=True)
    if res is None:
        raise HTTPException(502, "Выплата в обработке. Если звёзды не пришли, напиши в поддержку")
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        print("WITHDRAW FAIL:", desc, flush=True)
        async with lock:
            with tx():
                db.execute(f"UPDATE users SET balance=balance+?, withdrawn=withdrawn-?, wd_used={BIGGER}(0, wd_used-?) WHERE id=?",
                           (a, a, a, uid))
                db.execute("DELETE FROM ops WHERE cid=?", (cid,))
        raise HTTPException(400, next((m for k, m in WD_ERRORS.items() if k in desc), "Вывод не удался, попробуй позже"))
    return {"op": dict(k="wd", a=a, id=cid, ts=ts)}

def norm_code(x):
    c = (x or "").strip().upper()
    return c if re.fullmatch(r"[\w-]{3,20}", c) else ""

promo_fail = {}
def promo_limit(uid):
    t, n = promo_fail.get(uid, (0.0, 0))
    if time.time() - t <= 600 and n >= 5: raise HTTPException(429, "Слишком много неверных попыток. Подожди несколько минут")
def promo_miss(uid):
    t, n = promo_fail.get(uid, (0.0, 0)); now = time.time()
    if now - t > 600: t, n = now, 0
    promo_fail[uid] = (t, n + 1)

@app.post("/api/promo")
async def promo(request: Request):
    uid = auth(request); code = norm_code((await request.json()).get("code", ""))
    promo_limit(uid)
    if not code: raise HTTPException(400, "Введи промокод (3-20 символов)")
    async with lock:
        p = db.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
        if not p or not p["active"]:
            promo_miss(uid); raise HTTPException(400, "Промокод не найден")
        if db.execute("SELECT 1 FROM promo_uses WHERE code=? AND uid=?", (code, uid)).fetchone():
            raise HTTPException(400, "Ты уже активировал этот промокод")
        if p["used"] >= p["max_uses"]: raise HTTPException(400, "Промокод уже закончился")
        a = int(p["amount"]); cid = "pr" + secrets.token_hex(10); ts = int(time.time() * 1000)
        with tx():
            cur = db.execute("UPDATE promos SET used=used+1 WHERE code=? AND active=1 AND used<max_uses", (code,))
            if cur.rowcount == 0: raise HTTPException(400, "Промокод уже закончился")
            db.execute("INSERT INTO promo_uses(code,uid,ts) VALUES(?,?,?)", (code, uid, ts))
            db.execute("UPDATE users SET balance=balance+?, bonus=bonus+? WHERE id=?", (a, a, uid))
            db.execute("INSERT INTO ops(uid,k,g,a,cid,ts) VALUES(?,?,?,?,?,?)", (uid, "promo", code, a, cid, ts))
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    return {"amount": a, "balance": bal}

class Dep(StatesGroup):
    amount = State()

@dp.message(CommandStart())
async def start_cmd(m: Message, command: CommandObject, state: FSMContext):
    if command.args == "custom":
        await state.set_state(Dep.amount)
        await m.answer(f"Напиши, на сколько звёзд пополнить баланс (от 1 до {DEP_MAX:,}).".replace(",", " "))
    else:
        await m.answer("Открой мини-апп кнопкой меню.")

def is_owner(m: Message): return bool(OWNER_ID) and m.from_user is not None and m.from_user.id == OWNER_ID

@dp.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Твой id: {m.from_user.id}")

@dp.message(Command("newpromo"))
async def cmd_newpromo(m: Message, command: CommandObject):
    if not is_owner(m): return
    parts = (command.args or "").split()
    usage = ("Напиши так: /newpromo КОД СУММА АКТИВАЦИЙ\nПример: /newpromo START 500 100\n"
             "Вместо кода поставь -, тогда он создастся сам.")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit(): return await m.answer(usage)
    amount, uses = int(parts[1]), int(parts[2])
    if not 1 <= amount <= DEP_MAX or not 1 <= uses <= 100_000:
        return await m.answer(f"Сумма от 1 до {fmtn(DEP_MAX)}, активаций от 1 до 100 000.")
    code = secrets.token_hex(4).upper() if parts[0] == "-" else norm_code(parts[0])
    if not code: return await m.answer("Код: 3-20 символов, только буквы, цифры, - и _")
    if db.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone(): return await m.answer(f"Промокод {code} уже есть")
    db.execute("INSERT INTO promos(code,amount,max_uses,used,active,created) VALUES(?,?,?,?,?,?)", (code, amount, uses, 0, 1, int(time.time())))
    await m.answer(f"Готово: {code}\nДаёт {fmtn(amount)} ⭐, активаций: {fmtn(uses)}\nОтключить: /delpromo {code}")

@dp.message(Command("promos"))
async def cmd_promos(m: Message):
    if not is_owner(m): return
    rows = db.execute("SELECT code, amount, max_uses, used, active FROM promos ORDER BY created DESC LIMIT 30").fetchall()
    if not rows: return await m.answer("Промокодов пока нет. Создать: /newpromo START 500 100")
    await m.answer("\n".join(f"{r['code']} · {fmtn(r['amount'])} ⭐ · {r['used']}/{r['max_uses']}" + ("" if r["active"] else " · выключен") for r in rows))

@dp.message(Command("delpromo"))
async def cmd_delpromo(m: Message, command: CommandObject):
    if not is_owner(m): return
    code = norm_code(command.args or "")
    if not code or not db.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone(): return await m.answer("Напиши так: /delpromo КОД (такой код должен существовать)")
    db.execute("UPDATE promos SET active=0 WHERE code=?", (code,))
    await m.answer(f"Промокод {code} выключен. Кто уже активировал, звёзды оставит.")

@dp.message(Dep.amount, F.text)
async def custom_amount(m: Message, state: FSMContext):
    t = m.text.replace(" ", "")
    if not t.isdigit() or not 1 <= int(t) <= DEP_MAX:
        return await m.answer(f"Нужно целое число от 1 до {DEP_MAX}. Попробуй ещё раз.")
    a = int(t); await state.clear()
    await m.answer_invoice(title="Пополнение", description=f"{a} звёзд", payload=f"dep:{m.from_user.id}:{a}",
        currency="XTR", prices=[LabeledPrice(label="Пополнение", amount=a)])

@dp.pre_checkout_query()
async def pre(q: PreCheckoutQuery): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment; a = p.total_amount
    async with lock:
        db.execute("INSERT INTO users(id) VALUES(?) ON CONFLICT DO NOTHING", (m.from_user.id,))
        with tx():
            cur = db.execute("INSERT INTO ops(uid,k,a,cid,ts) VALUES(?,?,?,?,?) ON CONFLICT (cid) DO NOTHING",
                             (m.from_user.id, "dep", a, p.telegram_payment_charge_id, int(time.time() * 1000)))
            if cur.rowcount == 0: return
            db.execute("UPDATE users SET balance=balance+?, deposited=deposited+? WHERE id=?", (a, a, m.from_user.id))
    await m.answer(f"✅ Пополнение зачислено: {a} ⭐\nОткрой мини-апп, чтобы играть.")

@app.on_event("startup")
async def start(): asyncio.create_task(dp.start_polling(bot))

class CaseOpenRequest(BaseModel):
    case: str
    amount: int = 1
    instant: bool = False

@app.post("/api/cases/open")
async def open_case(request: Request, data: CaseOpenRequest):
    uid = auth(request)
    cid = data.case
    amount = max(1, min(5, data.amount))
    
    if cid == "daily":
        if uid != OWNER_ID:
            u = db.execute("SELECT daily_ts FROM users WHERE id=?", (uid,)).fetchone()
            last_ts = u["daily_ts"] if u and "daily_ts" in u.keys() else 0
            now = int(time.time())
            if now - last_ts < 86400:
                raise HTTPException(400, "Ежедневный кейс будет доступен позже")
            db.execute("UPDATE users SET daily_ts=? WHERE id=?", (now, uid))
        
        prizes = [
            {"k": "cup", "name": "Кубок", "value": 100, "ic": "🏆", "w": 4000},
            {"k": "ring", "name": "Кольцо", "value": 100, "ic": "💍", "w": 2000},
            {"k": "dia", "name": "Алмаз", "value": 100, "ic": "💎", "w": 1500},
            {"k": "sbear", "name": "Static-мишка", "value": 100, "ic": "🐻", "w": 1000},
            {"k": "lolly", "name": "Леденец", "value": 800, "ic": "🍭", "w": 700},
            {"k": "box", "name": "Подарок 2025", "value": 1000, "ic": "📦", "w": 400},
            {"k": "eskimo", "name": "Эскимо", "value": 2000, "ic": "🍦", "w": 200},
            {"k": "fist", "name": "Золотой кулак", "value": 2888, "ic": "💪", "w": 100},
            {"k": "book", "name": "Книга", "value": 3000, "ic": "📕", "w": 50},
        ]
        p = secrets.SystemRandom().choices(prizes, weights=[x["w"] for x in prizes])[0]
        price = 0
    else:
        c = None
        for cid2, title, p2, keys in CASES:
            if cid2 == cid:
                c = _build(cid2, title, p2, keys)
                break
        if not c: raise HTTPException(400, "Нет такого кейса")
        price = c["price"] * amount

    async with lock:
        s = state(uid)
        if s["balance"] < price: raise HTTPException(400, "Не хватает звёзд")
        gate(uid)
        
        with tx():
            if price > 0:
                cur = db.execute("UPDATE users SET balance=balance-?, played=played+?, wagered=wagered+? WHERE id=? AND balance>=?",
                                 (price, amount, price, uid, price))
                if cur.rowcount == 0: raise HTTPException(400, "Не хватает звёзд")
            
            items = []
            if cid == "daily":
                iid = secrets.token_hex(8)
                db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                           (iid, uid, cid, p["name"], p["value"], int(time.time() * 1000)))
                items.append(dict(id=iid, k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], case=cid))
            else:
                for _ in range(amount):
                    p = secrets.SystemRandom().choices(c["prizes"], weights=[x["w"] for x in c["prizes"]])[0]
                    iid = secrets.token_hex(8)
                    db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                               (iid, uid, cid, p["name"], p["value"], int(time.time() * 1000)))
                    items.append(dict(id=iid, k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], case=cid))
                
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    return {"items": items, "balance": bal}

@app.get("/api/inventory")
async def inventory(request: Request):
    uid = auth(request)
    rows = db.execute("SELECT id,case_id,name,value,ts FROM inv WHERE uid=? AND sold=0 ORDER BY ts DESC LIMIT 200", (uid,)).fetchall()
    return {"items": [dict(r) for r in rows], "balance": db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]}

@app.post("/api/inventory/sell")
async def sell(request: Request):
    uid = auth(request); ids = [str(i) for i in ((await request.json()).get("ids") or [])][:200]
    total = 0
    async with lock:
        with tx():
            for i in ids:
                if db.execute("UPDATE inv SET sold=1 WHERE id=? AND uid=? AND sold=0", (i, uid)).rowcount:
                    total += db.execute("SELECT value FROM inv WHERE id=?", (i,)).fetchone()["value"]
            if total:
                db.execute("UPDATE users SET balance=balance+?, won_total=won_total+? WHERE id=?", (total, total, uid))
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    return {"sold": total, "balance": bal}

@app.post("/api/upgrade_item")
async def upgrade_item(request: Request):
    uid = auth(request); j = await request.json(); iid = str(j.get("id") or ""); tk = j.get("target")
    if tk not in ITEMS: raise HTTPException(400, "Нет такого подарка")
    name, val, ic = ITEMS[tk]
    async with lock:
        row = db.execute("SELECT value FROM inv WHERE id=? AND uid=? AND sold=0", (iid, uid)).fetchone()
        if not row: raise HTTPException(400, "Подарок не найден")
        bp = int(0.9 * row["value"] / val * 10000)
        if val <= row["value"] or bp < 200 or bp > 7500:
            raise HTTPException(400, "Выбери подарок подороже: шанс должен быть от 2% до 75%")
        gate(uid)
        win = secrets.randbelow(10000) < bp
        new = None
        with tx():
            if db.execute("UPDATE inv SET sold=2 WHERE id=? AND uid=? AND sold=0", (iid, uid)).rowcount == 0:
                raise HTTPException(400, "Подарок не найден")
            db.execute("UPDATE users SET played=played+1, won=won+? WHERE id=?", (1 if win else 0, uid))
            if win:
                nid = secrets.token_hex(8)
                db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                           (nid, uid, "upgrade", name, val, int(time.time() * 1000)))
                new = dict(id=nid, k=tk, name=name, value=val, ic=ic, case="upgrade")
        bal = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
    return {"win": win, "item": new, "balance": bal}
