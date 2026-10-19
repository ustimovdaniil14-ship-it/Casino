# Кейсы с подарками. Шансы считаются так, чтобы игроку возвращалось ~86% цены кейса (остальное доход бота).
# Шансы и цены показываются игрокам открыто. Приз = подарок со своей ценой в звёздах (продаётся за неё).
import secrets, time
from fastapi import Request, HTTPException

RTP_TARGET = 0.86
OWNER_ID = 1780245250

ITEMS = {  # ключ: (название, цена в звёздах, значок)
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

# Дорогие кейсы: шансы заданы вручную (проценты, сумма = 100). Возврат считается из них и виден игроку.
FIXED = [
    ("magnate", "Магнат", 20000, "cat25:26 bear17:20 cat15:17 cap:11 car:7 bell:6 hat:4 watch:3 lamp:1.5 ufc:.5 heartc:4"),
    ("king", "Король", 30000, "heartc:24 cat25:30 bear17:18 cat15:16 cap:12"),
]

def _fixed(cid, title, price, spec):
    pairs = [(a.split(":")[0], float(a.split(":")[1])) for a in spec.split()]
    assert abs(sum(w for _, w in pairs) - 100) < 1e-6, cid
    prizes = [dict(k=k, name=ITEMS[k][0], value=ITEMS[k][1], ic=ITEMS[k][2], w=round(w * 1000)) for k, w in pairs]
    t = sum(p["w"] for p in prizes)
    return dict(id=cid, name=title, price=price, prizes=prizes, total=t,
                rtp=round(sum(p["value"] * p["w"] for p in prizes) / t / price, 3))

def _build(cid, title, price, keys):
    vals = [ITEMS[k][1] for k in keys]
    lo, hi = -1.0, 8.0
    for _ in range(80):   # чем выше k, тем чаще выпадают дешёвые призы
        k = (lo + hi) / 2; ws = [v ** -k for v in vals]
        if sum(w * v for w, v in zip(ws, vals)) / sum(ws) / price > RTP_TARGET: lo = k
        else: hi = k
    ws = [v ** -k for v in vals]; tot = sum(ws)
    prizes = [dict(k=x, name=ITEMS[x][0], value=ITEMS[x][1], ic=ITEMS[x][2], w=max(1, round(w / tot * 100000))) for x, w in zip(keys, ws)]
    t = sum(p["w"] for p in prizes)
    return dict(id=cid, name=title, price=price, prizes=prizes, total=t,
                rtp=round(sum(p["value"] * p["w"] for p in prizes) / t / price, 3))

CATALOG = {c[0]: _build(c[0], c[1], c[2], c[3].split()) for c in CASES}
CATALOG.update({c[0]: _fixed(*c) for c in FIXED})

# --- НАГРАДЫ ДЛЯ ЕЖЕДНЕВНОГО КЕЙСА ---
DAILY_PRIZES = [
    dict(k="cup", name="Кубок", value=100, ic="🏆", w=4000),
    dict(k="ring", name="Кольцо", value=100, ic="💍", w=2000),
    dict(k="dia", name="Алмаз", value=100, ic="💎", w=1500),
    dict(k="sbear", name="Static-мишка", value=100, ic="🐻", w=1000),
    dict(k="lolly", name="Леденец", value=800, ic="🍭", w=700),
    dict(k="box", name="Подарок 2025", value=1000, ic="📦", w=400),
    dict(k="eskimo", name="Эскимо", value=2000, ic="🍦", w=200),
    dict(k="fist", name="Золотой кулак", value=2888, ic="💪", w=100),
    dict(k="book", name="Книга", value=3000, ic="📕", w=50),
]

def setup(app, auth, db, tx, lock, PG, gate=lambda uid: None):
    big = "BIGINT" if PG else "INTEGER"
    db.execute(f"CREATE TABLE IF NOT EXISTS inv(id TEXT PRIMARY KEY, uid {big}, case_id TEXT, name TEXT,"
               f" value {big}, ts {big}, sold {big} DEFAULT 0)")
    db.execute("CREATE INDEX IF NOT EXISTS inv_uid ON inv(uid, sold)")

    def balance(uid): return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]

    @app.get("/api/cases")
    async def catalog():
        return {"gifts": [dict(k=k, name=v[0], value=v[1], ic=v[2]) for k, v in sorted(ITEMS.items(), key=lambda kv: kv[1][1])],
                "cases": [dict(id=c["id"], name=c["name"], price=c["price"], rtp=c["rtp"],
                               prizes=[dict(k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], p=p["w"] / c["total"]) for p in c["prizes"]])
                          for c in CATALOG.values()]}

    # --- ЕЖЕДНЕВНЫЙ КЕЙС ---
    @app.post("/api/cases/daily")
    async def open_daily(request: Request):
        uid = auth(request)
        row = db.execute("SELECT daily_ts FROM users WHERE id=?", (uid,)).fetchone()
        last_ts = row["daily_ts"] if row and "daily_ts" in row.keys() else 0
        now = int(time.time())
        
        if uid != OWNER_ID and now - last_ts < 86400:
            raise HTTPException(400, "Ежедневный кейс будет доступен позже")
            
        if uid != OWNER_ID:
            db.execute("UPDATE users SET daily_ts=? WHERE id=?", (now, uid))

        async with lock:
            with tx():
                p = secrets.SystemRandom().choices(DAILY_PRIZES, weights=[x["w"] for x in DAILY_PRIZES])[0]
                iid = secrets.token_hex(8)
                db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                           (iid, uid, "daily", p["name"], p["value"], int(time.time() * 1000)))
            bal = balance(uid)
        return {"items": [dict(id=iid, k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], case="daily")], "balance": bal}

    # --- ОБЫЧНЫЙ КЕЙС ---
    @app.post("/api/cases/open")
    async def open_case(request: Request):
        uid = auth(request); c = CATALOG.get((await request.json()).get("case"))
        if not c: raise HTTPException(400, "Нет такого кейса")
        async with lock:
            if balance(uid) < c["price"]: raise HTTPException(400, "Не хватает звёзд")
            gate(uid)   # пауза между открытиями
            with tx():
                cur = db.execute("UPDATE users SET balance=balance-?, played=played+1, wagered=wagered+?"
                                 " WHERE id=? AND balance>=?", (c["price"], c["price"], uid, c["price"]))
                if cur.rowcount == 0: raise HTTPException(400, "Не хватает звёзд")
                p = secrets.SystemRandom().choices(c["prizes"], weights=[x["w"] for x in c["prizes"]])[0]
                iid = secrets.token_hex(8)
                db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                           (iid, uid, c["id"], p["name"], p["value"], int(time.time() * 1000)))
            bal = balance(uid)
        return {"item": dict(id=iid, k=p["k"], name=p["name"], value=p["value"], ic=p["ic"], case=c["id"]), "balance": bal}

    @app.get("/api/inventory")
    async def inventory(request: Request):
        uid = auth(request)
        rows = db.execute("SELECT id,case_id,name,value,ts FROM inv WHERE uid=? AND sold=0 ORDER BY ts DESC LIMIT 200", (uid,)).fetchall()
        return {"items": [dict(r) for r in rows], "balance": balance(uid)}

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
            bal = balance(uid)
        return {"sold": total, "balance": bal}

    # Апгрейд подарков: свой подарок -> более дорогой. Шанс = 90% * цена_своего / цена_цели (от 2% до 75%), показывается игроку.
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
            bal = balance(uid)
        return {"win": win, "item": new, "balance": bal}
