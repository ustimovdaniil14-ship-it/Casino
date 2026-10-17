# pip install fastapi uvicorn aiogram==3.*
# BOT_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
import os, hmac, hashlib, json, time, secrets, asyncio, contextlib
from urllib.parse import parse_qsl
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

TOKEN = os.environ["BOT_TOKEN"]
RTP, DEP_MAX, WD_LIMIT = 0.9, 100_000, 50_000
# Если бот создан не в обычном Telegram, а на своём сервере приватки,
# задай переменную API_BASE (адрес Bot API приватки, например https://api.example.com)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
API_BASE = os.environ.get("API_BASE", "https://api.staticgram.top").rstrip("/")
session = AiohttpSession(api=TelegramAPIServer.from_base(API_BASE)) if API_BASE else None
bot, dp = Bot(TOKEN, session=session), Dispatcher()
# --- База данных ---
# DATABASE_URL задан  -> Postgres (бесплатные Neon / Supabase), данные не пропадают
# DATABASE_URL пуст   -> файл SQLite (на бесплатном Render он стирается при перезапуске!)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PG = DATABASE_URL.startswith("postgres")
if PG:
    import psycopg
    from psycopg.rows import dict_row
    def _connect():
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10)
else:
    import sqlite3
    def _connect():
        c = sqlite3.connect(os.environ.get("DB_PATH", "casino.db"), check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

class DB:
    def __init__(self): self.c = _connect()
    def execute(self, sql, params=()):
        if PG: sql = sql.replace("?", "%s")
        for attempt in (0, 1):
            try:
                return self.c.execute(sql, params)
            except Exception as e:
                # соединение с облачной базой могло закрыться — переподключаемся и пробуем ещё раз
                if PG and attempt == 0 and isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)):
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
):
    db.execute(stmt)

@contextlib.contextmanager
def tx():
    db.execute("BEGIN")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK"); raise
    else:
        db.execute("COMMIT")

lock = asyncio.Lock()

def auth(request: Request) -> int:
    raw = request.headers.get("X-Init-Data", "")
    d = dict(parse_qsl(raw, keep_blank_values=True)); h = d.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
        raise HTTPException(401, "bad initData")
    if time.time() - int(d.get("auth_date", 0)) > 86400: raise HTTPException(401, "expired")
    uid = json.loads(d["user"])["id"]
    db.execute("INSERT INTO users(id) VALUES(?) ON CONFLICT DO NOTHING", (uid,))
    return uid

def state(uid):
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    ops = [dict(k=r["k"], a=r["a"], b=r["b"], p=r["p"], x=r["x"], g=r["g"], id=r["cid"], ts=r["ts"])
           for r in db.execute("SELECT * FROM ops WHERE uid=? ORDER BY n DESC LIMIT 100", (uid,))]
    today = time.strftime("%Y-%m-%d")
    return dict(balance=u["balance"], deposited=u["deposited"], withdrawn=u["withdrawn"], wonTotal=u["won_total"],
                played=u["played"], won=u["won"], wagered=u["wagered"], best=u["best"], ops=ops,
                wd=dict(d=u["wd_day"] or today, u=u["wd_used"] if u["wd_day"] == today else 0))

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

@app.exception_handler(Exception)
async def _any(_, e):
    print("ERROR:", repr(e), flush=True)
    return JSONResponse({"error": f"{type(e).__name__}: {e}"[:200]}, 500)

@app.exception_handler(HTTPException)
async def _h(_, e): return JSONResponse({"error": e.detail}, e.status_code)

@app.get("/health")
async def health(): return {"ok": True}

@app.get("/")
async def index(): return FileResponse("index.html")  # мини-апп отдаётся с этого же сервера

@app.get("/api/state")
async def get_state(request: Request): return {"state": state(auth(request))}

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

# --- Вызов методов StaticGram, которых нет в aiogram (withdrawStarBalance и т.п.) ---
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
    """Автовывод: статики сразу отправляются игроку методом withdrawStarBalance (StaticGram)."""
    uid = auth(request); a = int((await request.json()).get("amount", 0))
    cid = "wd" + secrets.token_hex(12); ts = int(time.time() * 1000)   # он же request_id (идемпотентность)
    # 1) резервируем сумму на балансе игрока
    async with lock:
        s = state(uid)
        if a < 1 or a > s["balance"]: raise HTTPException(400, "Не хватает звёзд")
        if a > WD_LIMIT - s["wd"]["u"]: raise HTTPException(400, "Дневной лимит")
        with tx():
            db.execute("UPDATE users SET balance=balance-?, withdrawn=withdrawn+?, wd_day=?, wd_used=? WHERE id=?",
                       (a, a, s["wd"]["d"], s["wd"]["u"] + a, uid))
            db.execute("INSERT INTO ops(uid,k,a,cid,ts) VALUES(?,?,?,?,?)", (uid, "wd", a, cid, ts))
    # 2) отправляем звёзды игроку (при сбое связи повторяем с тем же request_id — двойной выплаты не будет)
    res = None
    for _ in range(2):
        try:
            res = await sg_call("withdrawStarBalance", user_id=uid, star_count=a, request_id=cid)
            break
        except Exception as e:
            print("WITHDRAW NET ERROR:", repr(e), flush=True)
    if res is None:  # связь пропала: деньги не возвращаем автоматически, чтобы не выплатить дважды
        raise HTTPException(502, "Выплата в обработке. Если звёзды не пришли, напиши в поддержку")
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        print("WITHDRAW FAIL:", desc, flush=True)
        async with lock:   # выплата отклонена — возвращаем сумму на баланс игрока
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

@app.post("/api/wheel")
async def wheel(request: Request):
    uid = auth(request); j = await request.json(); bet = int(j["bet"]); x = round(float(j["x"]), 2)
    if not 1.1 <= x <= 100: raise HTTPException(400, "Неверный множитель")
    async with lock:
        if bet < 1 or bet > state(uid)["balance"]: raise HTTPException(400, "Не хватает звёзд")
        win = secrets.SystemRandom().random() < RTP / x
        bal = play(uid, bet, int(bet * x) if win else 0, x, "wheel")
    return {"win": win, "balance": bal}

@app.post("/api/dice")
async def dice(request: Request):
    uid = auth(request); j = await request.json(); bet = int(j["bet"]); pick = j["pick"]
    if pick not in ("even", "odd"): raise HTTPException(400, "Неверный выбор")
    async with lock:
        if bet < 1 or bet > state(uid)["balance"]: raise HTTPException(400, "Не хватает звёзд")
        n = secrets.randbelow(6) + 1
        win = (pick == "even") == (n % 2 == 0)
        bal = play(uid, bet, int(bet * 1.9) if win else 0, 1.9, "dice")
    return {"n": n, "balance": bal}

# ---- бот: оплата звёздами ----
# ---- «Своё количество»: бот спрашивает сумму и присылает инвойс ----
class Dep(StatesGroup):
    amount = State()

@dp.message(CommandStart())
async def start_cmd(m: Message, command: CommandObject, state: FSMContext):
    if command.args == "custom":
        await state.set_state(Dep.amount)
        await m.answer(f"Напиши, на сколько звёзд пополнить баланс (от 1 до {DEP_MAX:,}).".replace(",", " "))
    else:
        await m.answer("Открой мини-апп кнопкой меню.")

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
            if cur.rowcount == 0: return  # этот платёж уже зачислен
            db.execute("UPDATE users SET balance=balance+?, deposited=deposited+? WHERE id=?", (a, a, m.from_user.id))
    await m.answer(f"✅ Пополнение зачислено: {a} ⭐\nОткрой мини-апп, чтобы играть.")

@app.on_event("startup")
async def start(): asyncio.create_task(dp.start_polling(bot))

# ---------------------------------------------------------------------------
# Работа с API StaticGram (счета, вывод, возвраты): используй документацию
# и библиотеку/описание методов на docs.staticgram.top
#   Оплата и вывод: https://docs.staticgram.top/guide/payments
#   Mini Apps:      https://docs.staticgram.top/guide/miniapps
# ---------------------------------------------------------------------------
