# Кейсы: подключается к server.py двумя строками (см. инструкцию). Шансы и цены открыты для игроков.
import secrets, time
from fastapi import Request, HTTPException

# (множитель ценности относительно цены кейса, вес). Возврат игроку ≈ 88%, остальное доход бота.
TIERS = [(0.2, 400), (0.6, 300), (1.2, 180), (2.4, 90), (5, 25), (12, 5)]
CASES = [
    ("start", "Обычный", 50, ["Конфета", "Звезда", "Открытка", "Мишка", "Сердце", "Кубок"]),
    ("silver", "Серебряный", 200, ["Букет", "Свеча", "Шкатулка", "Торт", "Корона", "Перстень"]),
    ("gold", "Золотой", 1000, ["Монета", "Слиток", "Медаль", "Часы", "Кристалл", "Скипетр"]),
    ("nft", "NFT", 3000, ["NFT Spark", "NFT Pixel", "NFT Orbit", "NFT Aurora", "NFT Dragon", "NFT Phoenix"]),
]
CATALOG = {}
for cid, title, price, names in CASES:
    prizes = [dict(name=n, value=round(price * m), w=w) for n, (m, w) in zip(names, TIERS)]
    tot = sum(p["w"] for p in prizes)
    CATALOG[cid] = dict(id=cid, name=title, price=price, prizes=prizes, total=tot,
                        rtp=round(sum(p["value"] * p["w"] for p in prizes) / tot / price, 3))

def setup(app, auth, db, tx, lock, PG, gate=lambda uid: None):
    big = "BIGINT" if PG else "INTEGER"
    db.execute(f"CREATE TABLE IF NOT EXISTS inv(id TEXT PRIMARY KEY, uid {big}, case_id TEXT, name TEXT,"
               f" value {big}, ts {big}, sold {big} DEFAULT 0)")
    db.execute("CREATE INDEX IF NOT EXISTS inv_uid ON inv(uid, sold)")

    def balance(uid): return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]

    @app.get("/api/cases")
    async def catalog():
        return {"cases": [dict(id=c["id"], name=c["name"], price=c["price"], rtp=c["rtp"],
                               prizes=[dict(name=p["name"], value=p["value"], p=p["w"] / c["total"]) for p in c["prizes"]])
                          for c in CATALOG.values()]}

    @app.post("/api/cases/open")
    async def open_case(request: Request):
        uid = auth(request); c = CATALOG.get((await request.json()).get("case"))
        if not c: raise HTTPException(400, "Нет такого кейса")
        async with lock:
            if balance(uid) < c["price"]: raise HTTPException(400, "Не хватает звёзд")
            gate(uid)   # пауза между ставками, как в играх
            with tx():
                cur = db.execute("UPDATE users SET balance=balance-?, played=played+1, wagered=wagered+?"
                                 " WHERE id=? AND balance>=?", (c["price"], c["price"], uid, c["price"]))
                if cur.rowcount == 0: raise HTTPException(400, "Не хватает звёзд")
                p = secrets.SystemRandom().choices(c["prizes"], weights=[x["w"] for x in c["prizes"]])[0]
                iid = secrets.token_hex(8)
                db.execute("INSERT INTO inv(id,uid,case_id,name,value,ts) VALUES(?,?,?,?,?,?)",
                           (iid, uid, c["id"], p["name"], p["value"], int(time.time() * 1000)))
            bal = balance(uid)
        return {"item": dict(id=iid, name=p["name"], value=p["value"], case=c["id"]), "balance": bal}

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
