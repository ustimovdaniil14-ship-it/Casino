# pip install fastapi uvicorn aiogram==3.*
# BOT_TOKEN=... uvicorn server:app --host 0.0.0.0 --port 8000
import os, hmac, hashlib, json, time, sqlite3, secrets, asyncio, contextlib
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
db = sqlite3.connect("casino.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, deposited INTEGER DEFAULT 0,
  withdrawn INTEGER DEFAULT 0, won_total INTEGER DEFAULT 0, played INTEGER DEFAULT 0, won INTEGER DEFAULT 0,
  wagered INTEGER DEFAULT 0, best INTEGER DEFAULT 0, wd_day TEXT DEFAULT '', wd_used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ops(n INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, k TEXT, a INTEGER, b INTEGER,
  p INTEGER, x REAL, g TEXT, cid TEXT UNIQUE, ts INTEGER);
""")
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
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
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

@app.post("/api/withdraw")
async def withdraw(request: Request):
    uid = auth(request); a = int((await request.json()).get("amount", 0))
    async with lock:
        s = state(uid)
        if a < 1 or a > s["balance"]: raise HTTPException(400, "Не хватает звёзд")
        if a > WD_LIMIT - s["wd"]["u"]: raise HTTPException(400, "Дневной лимит")
        cid = "wd" + secrets.token_hex(12); ts = int(time.time() * 1000)
        db.execute("UPDATE users SET balance=balance-?, withdrawn=withdrawn+?, wd_day=?, wd_used=? WHERE id=?",
                   (a, a, s["wd"]["d"], s["wd"]["u"] + a, uid))
        db.execute("INSERT INTO ops(uid,k,a,cid,ts) VALUES(?,?,?,?,?)", (uid, "wd", a, cid, ts))
        # тут твоя логика самого вывода (ручная обработка заявки / отправка звёзд)
    return {"op": dict(k="wd", a=a, id=cid, ts=ts)}

def play(uid, bet, payout, x, game):
    db.execute("UPDATE users SET balance=balance-?+?, played=played+1, wagered=wagered+?, won_total=won_total+?,"
               " won=won+?, best=MAX(best,?) WHERE id=?",
               (bet, payout, bet, payout, 1 if payout > bet else 0, max(0, payout - bet), uid))
    db.execute("INSERT INTO ops(uid,k,g,b,p,x,ts) VALUES(?,?,?,?,?,?,?)", (uid, "game", game, bet, payout, x, int(time.time() * 1000)))
    return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

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
        db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,))
        try:
            db.execute("INSERT INTO ops(uid,k,a,cid,ts) VALUES(?,?,?,?,?)",
                       (m.from_user.id, "dep", a, p.telegram_payment_charge_id, int(time.time() * 1000)))
        except sqlite3.IntegrityError: return  # этот платёж уже зачислен
        db.execute("UPDATE users SET balance=balance+?, deposited=deposited+? WHERE id=?", (a, a, m.from_user.id))
    await m.answer(f"✅ Пополнение зачислено: {a} ⭐\nОткрой мини-апп, чтобы играть.")

@app.on_event("startup")
async def start(): asyncio.create_task(dp.start_polling(bot))
