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
RTP, DEP_MAX, WD_LIMIT = 0.9, 10_000_000, 50_000
# Все лимиты можно менять на Render (Environment) без правки кода
def _env(name, default, cast=float): return cast(os.environ.get(name, default))
MAX_BET   = _env("MAX_BET", 5000, int)       # максимальная ставка за раунд
WIN_CAP   = _env("WIN_CAP", 50000, int)      # максимальный выигрыш за раунд (ставка × множитель)
COOLDOWN  = _env("COOLDOWN", 1.0)            # пауза между ставками одного игрока, сек (против скриптов и параллельных запросов)
OWNER_ID  = 1780245250                      # твой id в StaticGram: статистика и команды промокодов только для него
ROLLOVER  = _env("ROLLOVER", 1.0)            # вывод открывается, когда сумма ставок >= внесённое × ROLLOVER
def fmtn(n): return f"{int(n):,}".replace(",", " ")
def cap_err(): return HTTPException(400, f"Максимальный выигрыш за раунд {fmtn(WIN_CAP)} ⭐. Уменьши ставку или множитель")
def bet_err(): return HTTPException(400, f"Максимальная ставка {fmtn(MAX_BET)} ⭐")
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
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10,
                              prepare_threshold=None)   # без кэша планов: иначе после обновления таблиц бывает «cached plan must not change result type»
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
                # соединение с облачной базой могло закрыться: переподключаемся и повторяем,
                # но НЕ внутри транзакции, иначе запрос мог бы примениться дважды
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
    f"CREATE TABLE IF NOT EXISTS hub_users(id {INT} PRIMARY KEY, first_seen {INT}, last_seen {INT})",
    f"CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount {INT}, max_uses {INT}, used {INT} DEFAULT 0, active {INT} DEFAULT 1, created {INT})",
    f"CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, uid {INT}, ts {INT}, PRIMARY KEY(code, uid))",
):
    db.execute(stmt)

try:   # бонусные звёзды от промокодов (для условия по обороту при выводе)
    db.execute(f"ALTER TABLE users ADD COLUMN bonus {INT} DEFAULT 0")
except Exception:
    pass

db.execute(f"CREATE TABLE IF NOT EXISTS gifts(oid TEXT PRIMARY KEY, bid TEXT, k TEXT, uid {INT}, ts {INT})")
db.execute("CREATE INDEX IF NOT EXISTS gifts_uid ON gifts(uid, k)")
try: db.execute("ALTER TABLE gifts ADD COLUMN st TEXT")   # st='q' = вывод в очереди (касса ещё не может передать)
except Exception: pass
try: db.execute("ALTER TABLE gifts ADD COLUMN ident TEXT")   # k=UNM = подарок игрока ещё не сопоставлен с каталогом (ждёт /map)
except Exception: pass
db.execute("CREATE TABLE IF NOT EXISTS gmap(ident TEXT PRIMARY KEY, k TEXT)")   # ручное сопоставление: id/название подарка -> наш ключ

# Мины удалены: возвращаем ставки незаконченных игр и убираем таблицу
try:
    for r in db.execute("SELECT uid, bet FROM mines").fetchall():
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (r["bet"], r["uid"]))
    db.execute("DROP TABLE mines")
except Exception:
    pass

@contextlib.contextmanager
def tx():
    db.execute("BEGIN"); db.in_tx = True
    try:
        yield
    except BaseException:
        try: db.execute("ROLLBACK")
        except Exception: db.c = _connect()   # связь оборвалась: открытая транзакция всё равно откатится на стороне базы
        raise
    else:
        db.execute("COMMIT")
    finally:
        db.in_tx = False

lock = asyncio.Lock()

seen = {}   # когда игрок последний раз обращался к серверу (для счётчика онлайна)
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
    nowms = int(time.time() * 1000)
    db.execute("INSERT INTO hub_users(id,first_seen,last_seen) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen", (uid, nowms, nowms))
    return uid

last_bet = {}   # когда игрок делал последнюю ставку
def gate(uid):
    """Не чаще одной ставки в COOLDOWN секунд на игрока: останавливает скрипты и пачки параллельных запросов."""
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
async def index(): return FileResponse("index.html")  # мини-апп отдаётся с этого же сервера

@app.get("/api/state")
async def get_state(request: Request):
    uid = auth(request)
    return {"state": state(uid), "online": online()}

@app.get("/api/stats")
async def get_stats(request: Request):
    uid = auth(request)
    if uid != OWNER_ID: raise HTTPException(403, "Нет доступа")
    tz = 4 * 3600   # Баку (UTC+4): «сегодня» считается по местному времени
    t = int(time.time()); day0 = ((t + tz) // 86400 * 86400 - tz) * 1000
    total = db.execute("SELECT COUNT(*) AS c FROM hub_users").fetchone()["c"]
    today = db.execute("SELECT COUNT(*) AS c FROM hub_users WHERE first_seen>=?", (day0,)).fetchone()["c"]
    return {"total": total, "online": online(), "today": today}

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
        need = int((s["deposited"] + s["bonus"]) * ROLLOVER - s["wagered"] + .999)   # бонусы от промокодов тоже нужно отыграть
        if need > 0: raise HTTPException(400, f"Чтобы вывести, сыграй ещё на {fmtn(need)} ⭐ ставок")
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

# ---- Промокоды ----
def norm_code(x):
    c = (x or "").strip().upper()
    return c if re.fullmatch(r"[\w-]{3,20}", c) else ""

promo_fail = {}   # защита от подбора кодов: uid -> (время первой ошибки, число ошибок)
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


# ---- Промокоды: создание только для владельца (OWNER_ID) ----
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

# ---- Business-подключение: бот управляет подарками аккаунта-кассы ----
from aiogram.types import BusinessConnection
BIZ = {"id": os.environ.get("BIZ_ID", ""), "user": int(os.environ.get("BIZ_USER", 0))}   # id подключения; можно задать в Environment, чтобы не терялся при перезапуске

@dp.business_connection()
async def biz_conn(c: BusinessConnection):
    if c.is_enabled: BIZ["id"] = c.id; BIZ["user"] = c.user.id
    print("BUSINESS CONNECTION:", c.id, c.user.id, c.is_enabled, flush=True)
    if OWNER_ID: await bot.send_message(OWNER_ID, f"Business: {'подключено' if c.is_enabled else 'отключено'}\nBIZ_ID: {c.id}\nBIZ_USER: {c.user.id}\nСохрани оба в Environment, иначе после перезапуска вернутся старые")

@dp.message(Command("wipe"))
async def cmd_wipe(m: Message, command: CommandObject):
    """Владелец: стереть ВСЕ записи о подарках (склад, инвентари, очередь). Нужно после смены аккаунта кассы."""
    if not is_owner(m): return
    n = db.execute("SELECT COUNT(*) AS c FROM gifts").fetchone()["c"]
    if (command.args or "").strip() != "yes": return await m.answer(f"Это удалит записи обо всех подарках ({n} шт): склад, инвентари игроков, очередь вывода. Сами подарки на аккаунте не трогаются.\nПодтверди: /wipe yes")
    db.execute("DELETE FROM gifts"); UNK.clear()
    await m.answer(f"Удалено записей: {n}. Теперь /sync")

@dp.message(Command("gifts"))
async def cmd_gifts(m: Message):
    """Только владелец: показывает подарки, лежащие на аккаунте-кассе."""
    if not is_owner(m): return
    if not BIZ["id"]: return await m.answer("Нет подключения. Подключи бота: Настройки → Business → Чат-боты")
    r = await sg_call("getBusinessAccountGifts", business_connection_id=BIZ["id"], limit=20)
    await m.answer(json.dumps(r, ensure_ascii=False)[:3800] or "пусто")

# ---- NFT: касса, инвентарь игроков, наличие ----
# Таблица gifts = подарки, которые физически лежат на аккаунте-кассе.
# uid = владелец-игрок; uid IS NULL = склад (то, что можно выиграть); uid < 0 = вывод в процессе.
GID = {"9000000000000001": "heart", "9000000000000002": "bear", "9000000000000003": "gift", "9000000000000004": "rose",
       "9000000000000005": "cake", "9000000000000006": "bouq", "9000000000000007": "rocket", "9000000000000008": "cup",
       "9000000000000009": "ring", "9000000000000010": "dia", "9000000000000011": "champ", "9000000000000042": "sbear"}
VAL = {"heart": 15, "bear": 15, "gift": 25, "rose": 25, "cake": 50, "bouq": 50, "rocket": 50, "champ": 50, "cup": 100, "ring": 100,
       "dia": 100, "sbear": 100, "box": 2499, "eskimo": 2500, "lolly": 2699, "cane": 3000, "bday": 3000, "fist": 4499, "lamp": 4800,
       "pack": 5000, "ufc": 6000, "tile": 7500, "crystal": 7997, "helmet": 8500, "voodoo": 8888, "cal": 9500, "bell": 10000,
       "statue": 10000, "car": 10000, "hat": 10500, "gring": 11000, "watch": 12000, "book": 12500, "glass": 15000, "cigar": 15500,
       "cap": 33333, "cat15": 47777, "bear17": 55000, "cat25": 55555, "heartc": 60180}
UP_RTP = 0.81
UNM = "_unm"   # метка k для подарка игрока, ещё не сопоставленного с каталогом
CASHIER = os.environ.get("CASHIER", "").lstrip("@")   # username аккаунта-кассы (для подсказки «куда отправлять подарок»)
UNK = {}   # что не распознано при последнем /sync или входящем подарке: ident -> образец

def gcands(g):
    """Все возможные идентификаторы подарка (у обычных id, у NFT чаще base_name / gift_id)."""
    g = g or {}
    return [str(g[f]) for f in ("id", "base_name", "gift_id", "title", "name") if g.get(f) not in (None, "")]

def gresolve(g):
    c = gcands(g)
    for i in c:
        k = GID.get(i)
        if k: return k, (c[0] if c else "")
    if c:
        for r in db.execute("SELECT ident, k FROM gmap").fetchall():
            if r["ident"] in c and r["k"] in VAL: return r["k"], c[0]
    return None, (c[0] if c else "")

def add_gift(oid, bid, k, uid, ident=None):
    cur = db.execute("INSERT INTO gifts(oid,bid,k,uid,ts,ident) VALUES(?,?,?,?,?,?) ON CONFLICT (oid) DO NOTHING",
                     (oid, bid, k, uid, int(time.time() * 1000), ident))
    return cur.rowcount

@dp.business_message()
async def biz_msg(m: Message):
    """Игрок подарил подарок кассе: зачисляем в его инвентарь (обычные и NFT)."""
    d = m.model_dump(mode="json", exclude_none=True)
    g = d.get("gift") or d.get("unique_gift")
    if g is None:
        if not m.text and OWNER_ID: await bot.send_message(OWNER_ID, "BUSINESS MSG:\n" + json.dumps(d, ensure_ascii=False)[:3800])
        return
    sender = m.from_user.id if m.from_user else 0
    if sender == BIZ.get("user"): return          # исходящее с самой кассы
    oid = g.get("owned_gift_id"); k, ident = gresolve(g.get("gift"))
    if not oid or not k:
        UNK[ident or "?"] = g
        sender_ = m.from_user.id if m.from_user else 0
        if oid and sender_ != OWNER_ID:   # подарок игрока не теряем: храним как «?», зачислится после /map
            async with lock:
                db.execute("INSERT INTO users(id) VALUES(?) ON CONFLICT DO NOTHING", (sender_,))
                add_gift(str(oid), m.business_connection_id or BIZ["id"], UNM, sender_, ident or "?")
            with contextlib.suppress(Exception): await bot.send_message(sender_, "Подарок получен. Он появится в инвентаре после проверки.")
        if OWNER_ID: await bot.send_message(OWNER_ID, f"Неизвестный подарок (не зачислен), ident: {ident or '?'}\nСопоставить: /map {ident or 'ID'} <ключ>\n\n" + json.dumps(d, ensure_ascii=False)[:3300])
        return
    uid = None if sender == OWNER_ID else sender       # подарок от владельца = пополнение склада
    async with lock:
        if uid: db.execute("INSERT INTO users(id) VALUES(?) ON CONFLICT DO NOTHING", (uid,))
        n = add_gift(str(oid), m.business_connection_id or BIZ["id"], k, uid)
    if n and uid:
        with contextlib.suppress(Exception):
            await bot.send_message(uid, f"✅ Подарок зачислен в твой инвентарь ({k}, {VAL[k]} ⭐). Открой мини-апп.")

@dp.message(Command("sync"))
async def cmd_sync(m: Message):
    """Владелец: всё, что лежит на кассе и ещё не учтено, добавить на склад."""
    if not is_owner(m): return
    if not BIZ["id"]: return await m.answer("Нет подключения")
    added = unk = 0; off = ""; UNK.clear()
    for _ in range(20):
        r = await sg_call("getBusinessAccountGifts", business_connection_id=BIZ["id"], limit=100, offset=off)
        if not r.get("ok"): return await m.answer("Ошибка: " + json.dumps(r, ensure_ascii=False)[:500])
        res = r["result"]
        for it in res.get("gifts", []):
            oid = it.get("owned_gift_id"); k, ident = gresolve(it.get("gift"))
            if oid is None or not k:
                unk += 1; UNK.setdefault(ident or "?", it); continue
            async with lock: added += add_gift(str(oid), BIZ["id"], k, None)
        off = res.get("next_offset") or ""
        if not off: break
    await m.answer(f"Добавлено на склад: {added}. Не распознано: {unk}" + ("\nПосмотреть: /unknown" if unk else ""))

@dp.message(Command("stock"))
async def cmd_stock(m: Message):
    if not is_owner(m): return
    rows = db.execute("SELECT k, COUNT(*) c FROM gifts WHERE uid IS NULL GROUP BY k ORDER BY k").fetchall()
    await m.answer("Склад:\n" + ("\n".join(f"{r['k']}: {r['c']}" for r in rows) or "пусто"))

@dp.message(Command("unknown"))
async def cmd_unknown(m: Message):
    """Владелец: что не распознано (после /sync): по одной короткой строке на каждый ident."""
    if not is_owner(m): return
    if not UNK: return await m.answer("Нераспознанного нет (или сначала сделай /sync)")
    out = []
    for i, v in UNK.items():
        g = v.get("gift", v) if isinstance(v, dict) else {}
        out.append(f"{i} | id={g.get('gift_id') or g.get('id') or '?'} | модель={(g.get('model') or {}).get('name', '-')} | ⭐={g.get('star_count') or v.get('transfer_star_count') or v.get('star_count') or '-'}")
    await m.answer(f"Не распознано, уникальных: {len(UNK)}\nСопоставить: /map <ident> <ключ>\n\n" + "\n".join(out)[:3800])

@dp.message(Command("map"))
async def cmd_map(m: Message, command: CommandObject):
    if not is_owner(m): return
    p = (command.args or "").rsplit(None, 1)
    if len(p) != 2 or p[1] not in VAL: return await m.answer("Формат: /map <ident> <ключ>\nКлючи: " + ", ".join(VAL))
    db.execute("DELETE FROM gmap WHERE ident=?", (p[0],)); db.execute("INSERT INTO gmap(ident,k) VALUES(?,?)", (p[0], p[1]))
    rows = db.execute("SELECT oid, uid FROM gifts WHERE k=? AND ident=?", (UNM, p[0])).fetchall()
    for r in rows: db.execute("UPDATE gifts SET k=?, ident=NULL WHERE oid=?", (p[1], r["oid"]))
    for r in rows:
        if r["uid"] and r["uid"] > 0:
            with contextlib.suppress(Exception): await bot.send_message(r["uid"], f"✅ Подарок зачислен в твой инвентарь ({p[1]}, {VAL[p[1]]} ⭐). Открой мини-апп.")
    await m.answer(f"Ок: {p[0]} → {p[1]}. Зачислено подарков игроков: {len(rows)}. Остальное с кассы: /sync")

@dp.message(Command("queue"))
async def cmd_queue(m: Message):
    if not is_owner(m): return
    rows = db.execute("SELECT oid, k, uid FROM gifts WHERE st='q'").fetchall()
    await m.answer("Очередь вывода:\n" + ("\n".join(f"{r['oid']} · {r['k']} → {-r['uid']}" for r in rows) or "пусто"))

@dp.message(Command("retry"))
async def cmd_retry(m: Message):
    if not is_owner(m): return
    ok, wait, bad = await retry_queue()
    await m.answer(f"Выведено: {ok}, ждут: {wait}, вернул игрокам из-за ошибки: {bad}")

@dp.message(Command("give"))
async def cmd_give(m: Message, command: CommandObject):
    """Тест вывода: /give <owned_gift_id> <user_id>. Реально передаёт подарок!"""
    if not is_owner(m): return
    try: oid, to = (command.args or "").split(); to = int(to)
    except Exception: return await m.answer("Формат: /give <owned_gift_id> <user_id>")
    if not BIZ["id"]: return await m.answer("BIZ_ID не задан: подключи бота или добавь BIZ_ID в Environment")
    r = await sg_call("transferGift", business_connection_id=BIZ["id"], owned_gift_id=oid, new_owner_chat_id=to)
    if r.get("ok"): db.execute("DELETE FROM gifts WHERE oid=?", (oid,))
    await m.answer(json.dumps(r, ensure_ascii=False)[:3500])

@app.get("/api/stock")
async def api_stock(request: Request):
    auth(request)
    rows = db.execute("SELECT k, COUNT(*) c FROM gifts WHERE uid IS NULL GROUP BY k").fetchall()
    return {"stock": {r["k"]: r["c"] for r in rows}, "cashier": CASHIER}

@app.get("/api/inventory")
async def api_inventory(request: Request):
    uid = auth(request)
    rows = db.execute("SELECT oid, k, uid FROM gifts WHERE uid IN (?,?) AND (uid>0 OR st='q') AND k<>? ORDER BY ts DESC", (uid, -uid, UNM)).fetchall()
    return {"items": [dict(id=r["oid"], k=r["k"], value=VAL.get(r["k"], 0), pending=r["uid"] < 0) for r in rows]}

@app.post("/api/upgrade_item")
async def api_upgrade(request: Request):
    uid = auth(request); gate(uid); b = await request.json(); t = b.get("target")
    if t not in VAL: raise HTTPException(400, "Неизвестный подарок")
    async with lock:
        src = db.execute("SELECT oid, k FROM gifts WHERE oid=? AND uid=? AND k<>?", (str(b.get("id")), uid, UNM)).fetchone()
        if not src: raise HTTPException(400, "Подарок не найден")
        s, d = VAL[src["k"]], VAL[t]; bp = int(UP_RTP * s / d * 10000)
        if d <= s or not 200 <= bp <= 7500: raise HTTPException(400, "Так апгрейдить нельзя")
        tgt = db.execute("SELECT oid FROM gifts WHERE k=? AND uid IS NULL LIMIT 1", (t,)).fetchone()
        if not tgt: raise HTTPException(400, "Этого подарка сейчас нет в наличии")
        roll = secrets.randbelow(10000); win = roll < bp
        with tx():
            db.execute("UPDATE gifts SET uid=NULL WHERE oid=?", (src["oid"],))      # исходный уходит на склад в любом случае
            if win: db.execute("UPDATE gifts SET uid=? WHERE oid=?", (uid, tgt["oid"]))
            db.execute("INSERT INTO ops(uid,k,g,b,p,x,ts) VALUES(?,?,?,?,?,?,?)", (uid, "game", "upgrade", s, d if win else 0, d / s, int(time.time() * 1000)))
    return {"win": win, "roll": roll / 10000, "item": dict(id=tgt["oid"], k=t, value=d) if win else None}

@app.post("/api/withdraw_gift")
async def api_withdraw_gift(request: Request):
    """Вывод подарка игроку: резервируем в БД, передаём через transferGift, при отказе возвращаем."""
    uid = auth(request); gate(uid); oid = str((await request.json()).get("id"))
    async with lock:
        row = db.execute("SELECT oid, bid FROM gifts WHERE oid=? AND uid=? AND k<>?", (oid, uid, UNM)).fetchone()
        if not row: raise HTTPException(400, "Подарок не найден")
        if not (row["bid"] or BIZ["id"]): raise HTTPException(503, "Касса не подключена")
        db.execute("UPDATE gifts SET uid=? WHERE oid=?", (-uid, oid))
    try: res = await sg_call("transferGift", business_connection_id=row["bid"] or BIZ["id"], owned_gift_id=oid, new_owner_chat_id=uid)
    except Exception as e:
        print("GIFT WD NET ERROR:", repr(e), flush=True)
        raise HTTPException(502, "Вывод в обработке. Если подарок не пришёл, напиши в поддержку")   # остаётся зарезервированным, чтобы не выдать дважды
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        print("GIFT WD FAIL:", res, flush=True)
        if "TOO_NEW" in desc:   # подключению кассы ещё нет нужного срока: оставляем в резерве, фон передаст позже
            async with lock: db.execute("UPDATE gifts SET st='q' WHERE oid=?", (oid,))
            if OWNER_ID: await bot.send_message(OWNER_ID, f"Заявка на вывод в очереди: {oid} → {uid}\nОтправится сама, как только передача станет доступна (/retry — проверить сейчас)")
            return {"ok": True, "queued": True}
        async with lock: db.execute("UPDATE gifts SET uid=? WHERE oid=?", (uid, oid))
        raise HTTPException(400, "Вывод не удался, попробуй позже")
    async with lock:
        db.execute("DELETE FROM gifts WHERE oid=?", (oid,))
        db.execute("INSERT INTO ops(uid,k,g,ts) VALUES(?,?,?,?)", (uid, "gwd", oid, int(time.time() * 1000)))
    return {"ok": True}


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

rlock = asyncio.Lock()
async def retry_queue():
    """Пробует передать подарки из очереди. Возвращает (выведено, ждут, возвращено игроку)."""
    ok = wait = bad = 0
    async with rlock:
        for r in db.execute("SELECT oid, bid, uid FROM gifts WHERE st='q'").fetchall():
            to = -r["uid"]
            try: res = await sg_call("transferGift", business_connection_id=r["bid"] or BIZ["id"], owned_gift_id=r["oid"], new_owner_chat_id=to)
            except Exception as e:
                print("RETRY NET ERROR:", repr(e), flush=True); wait += 1; continue
            if res.get("ok"):
                async with lock:
                    db.execute("DELETE FROM gifts WHERE oid=?", (r["oid"],))
                    db.execute("INSERT INTO ops(uid,k,g,ts) VALUES(?,?,?,?)", (to, "gwd", r["oid"], int(time.time() * 1000)))
                ok += 1
                with contextlib.suppress(Exception): await bot.send_message(to, "✅ Подарок выведен на твой аккаунт")
            elif "TOO_NEW" in str(res.get("description", "")): wait += 1
            else:
                print("RETRY FAIL:", res, flush=True)
                async with lock: db.execute("UPDATE gifts SET uid=?, st=NULL WHERE oid=?", (to, r["oid"]))
                bad += 1
                with contextlib.suppress(Exception): await bot.send_message(to, "Вывод подарка не удался, он вернулся в инвентарь")
    return ok, wait, bad

async def retry_loop():
    while True:
        await asyncio.sleep(600)
        try: await retry_queue()
        except Exception as e: print("RETRY LOOP ERROR:", repr(e), flush=True)

@app.on_event("startup")
async def start():
    asyncio.create_task(dp.start_polling(bot)); asyncio.create_task(retry_loop())

# ---------------------------------------------------------------------------
# Работа с API StaticGram (счета, вывод, возвраты): используй документацию
# и библиотеку/описание методов на docs.staticgram.top
#   Оплата и вывод: https://docs.staticgram.top/guide/payments
#   Mini Apps:      https://docs.staticgram.top/guide/miniapps
# ---------------------------------------------------------------------------

# ---- Кейсы (файл cases.py лежит рядом) ----
try:
    import cases
    cases.setup(app, auth, db, tx, lock, PG, gate)
except ImportError:
    print("cases.py не найден, кейсы отключены", flush=True)
