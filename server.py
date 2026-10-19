# pip install fastapi uvicorn aiogram==3.*
# BOT_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
import os, re, hmac, hashlib, json, time, secrets, asyncio, contextlib
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
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
OWNER_ID  = 1780245250  # ТВОЙ ID ВПИСАН СЮДА
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
):
    db.execute(stmt)

try: db.execute(f"ALTER TABLE users ADD COLUMN bonus {INT} DEFAULT 0")
except Exception: pass

try:
    for r in db.execute("SELECT uid, bet FROM mines").fetchall():
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (r["bet"], r["uid"]))
    db.execute("DROP TABLE mines")
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
    return dict(balance=u["balance"], deposited=u["deposited"], withdrawn=u["withdrawn"], wonTotal=u["won_total"],
                played=u["played"], won=u["won"], wagered=u["wagered"], best=u["best"], bonus=u["bonus"] or 0, ops=ops,
                wd=dict(d=u["wd_day"] or today, u=u["wd_used"] if u["wd_day"] == today else 0))

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
    if OWNER_ID and uid != OWNER_ID:
        raise HTTPException(403, "Нет доступа")
    now = int(time.time())
    total = db.execute("SELECT COUNT(*) c FROM hub_users").fetchone()["c"]
    online_cnt = online()
    today_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    today = db.execute("SELECT COUNT(*) c FROM hub_users WHERE first_seen>=?", (today_start,)).fetchone()["c"]
    return {"total": total, "online": online_cnt, "today": today}

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

def play(uid, bet, payout, x, game):
    with tx():
        db.execute("UPDATE users SET balance=balance-?+?, played=played+1, wagered=wagered+?, won_total=won_total+?,"
                   f" won=won+?, best={BIGGER}(best,?) WHERE id=?",
                   (bet, payout, bet, payout, 1 if payout > bet else 0, max(0, payout - bet), uid))
        db.execute("INSERT INTO ops(uid,k,g,b,p,x,ts) VALUES(?,?,?,?,?,?,?)", (uid, "game", game, bet, payout, x, int(time.time() * 1000)))
    return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]

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

import cases
cases.setup(app, auth, db, tx, lock, PG, gate)
