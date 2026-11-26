#!/usr/bin/env python3
"""OneDayOnly new-product monitor. Stdlib only. Detects NEW product IDs only (no hashing of page content)."""
import os, re, sys, json, time, shutil, sqlite3, tempfile, urllib.request, urllib.parse, urllib.error
from html.parser import HTMLParser

HOST = "onedayonly.co.za"
BASE = "https://www.onedayonly.co.za"
DB = os.environ.get("ODO_DB", "monitor.db")
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
UA = "Mozilla/5.0 (compatible; personal-deal-monitor/1.0)"
KEEP_PARAMS = {"id", "p", "pid", "product", "product_id", "deal", "dealid", "sku"}
SEED_TEXT = {"today's deals", "extra time", "new", "clearance", "home & garden", "apparel & accessories",
    "health & beauty", "food & beverages", "furniture", "shoes", "sporting goods", "electronics", "hardware",
    "media", "toys & games", "luggage & bags", "arts & entertainment", "office supplies", "baby & toddler",
    "animals & pet supplies", "vehicles & parts", "business & industrial", "cameras & optics", "black friday",
    "onenightonly", "101"}
EXCLUDE = re.compile(r"^/(about|blog|terms|privacy|help|contact|careers|account|my-?account|my-?orders|orders|track|"
    r"returns|sell|subscribe|competitions|gift-?vouchers?|login|register|cart|checkout|banking|report|brands?)\b", re.I)
STEAL = re.compile(os.environ.get("STEAL_PATTERN", r"verified[\s_-]*steal"), re.I)
PRICE = re.compile(r"R\s?(\d[\d,\s]*)")
SAVE = re.compile(r"save\s*-\s*(\d+)\s*%", re.I)
MIN_STEAL_PCT = int(os.environ.get("STEAL_MIN_PCT", "80"))  # steal = this % off or more
MAX_PAGES_PER_SEED, MAX_SEEDS = 30, 40

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

# ---------- fetching ----------
def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403):
                break  # never try to bypass access controls
        except Exception as e:
            last = e
        time.sleep(2 ** (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")

# ---------- parsing ----------
class Anchors(HTMLParser):
    def __init__(s):
        super().__init__(); s.out = []; s.cur = None; s.skip = 0
    def handle_starttag(s, t, a):
        a = dict(a)
        if t in ("style", "script"): s.skip += 1
        if t == "a" and a.get("href"):
            s.cur = {"href": a["href"], "text": [], "img": None, "alt": None, "raw": []}
        if s.cur is not None:
            s.cur["raw"].append(" ".join(str(v) for k, v in a.items() if k in ("class", "alt", "title", "src", "data-src", "aria-label", "data-badge") and v))
        if t == "img" and s.cur is not None:
            src = a.get("data-src") or a.get("src") or ""
            if src and not s.cur["img"]: s.cur["img"] = src
            s.cur["alt"] = s.cur["alt"] or a.get("alt")
    def handle_data(s, d):
        if s.cur is not None and not s.skip and d.strip(): s.cur["text"].append(d.strip())
    def handle_endtag(s, t):
        if t in ("style", "script") and s.skip: s.skip -= 1
        if t == "a" and s.cur is not None:
            s.cur["text"] = " ".join(s.cur["text"]); s.cur["raw"] = " ".join(s.cur["raw"]); s.out.append(s.cur); s.cur = None

def norm(href, base=BASE + "/"):
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, href))
    if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") != HOST: return None
    q = sorted((k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() in KEEP_PARAMS)
    path = re.sub(r"/+$", "", p.path) or "/"
    return urllib.parse.urlunsplit(("https", "www." + HOST, path, urllib.parse.urlencode(q), ""))

def product_id(url):
    m = re.search(r"/(\d{5,})(?:/|$)", urllib.parse.urlsplit(url).path)
    return "id:" + m.group(1) if m else "url:" + url

def money(s): return int(re.sub(r"\D", "", s))

def parse_items(html, base=BASE + "/"):
    p = Anchors(); p.feed(html); items = {}
    for a in p.out:
        url = norm(a["href"], base)
        if not url: continue
        path = urllib.parse.urlsplit(url).path
        txt = a["text"] or a["alt"] or ""
        if not path.startswith("/products/") or len(path) < 12: continue
        clean = re.sub(r"save\s*-\s*(?:R\s?[\d,]+|\d+\s*%)", "", txt, flags=re.I)  # drop "save - R12,000 / 49%"
        prices = [money(x) for x in PRICE.findall(clean)]
        sv = SAVE.search(txt)
        pct = int(sv.group(1)) if sv else None
        if pct is None and len(prices) > 1 and prices[1] > prices[0]: pct = round(100 * (prices[1] - prices[0]) / prices[1])
        steal = bool((pct or 0) >= MIN_STEAL_PCT or STEAL.search(txt) or STEAL.search(a["raw"]))
        name = re.sub(r"(save\s*-\s*[\dR%,.]+|more options|R\s?\d[\d,\s]*)", "", STEAL.sub("", txt), flags=re.I).strip(" -")
        if not name or re.search(r"[{};]", name) or name.lower() in ("best seller", "more options", "new"):
            name = re.sub(r"-\d{6,}(?:-\d+)?$", "", path.rsplit("/", 1)[-1]).replace("-", " ").title()
        img = urllib.parse.urljoin(base, a["img"]) if a["img"] else None
        items.setdefault(product_id(url), {"url": url, "name": name[:150], "price": prices[0] if prices else None,
            "was": prices[1] if len(prices) > 1 else None, "pct": pct, "steal": steal, "image": img})
    return items, p.out

def find_seeds(anchors):
    seeds = [BASE + "/"]
    for a in anchors:
        u = norm(a["href"])
        if u and a["text"].lower() in SEED_TEXT and not a["img"] and u not in seeds: seeds.append(u)
    return seeds[:MAX_SEEDS]

def robots_ok(fetcher):
    try: txt = fetcher(BASE + "/robots.txt")
    except Exception: return lambda path: True
    rules, on = [], False
    for line in txt.splitlines():
        k, _, v = line.partition(":"); k, v = k.strip().lower(), v.strip()
        if k == "user-agent": on = v == "*"
        elif k == "disallow" and on and v: rules.append(v)
    return lambda path: not any(path.startswith(r) for r in rules)

def crawl(fetcher, delay=1.0):
    """Today's Deals = the homepage (its 'Today's Deals' tab links to '/'). Raises on any failure."""
    robots_ok(fetcher)
    items, _ = parse_items(fetcher(BASE + "/"))
    if not items: raise RuntimeError("catalogue extraction returned 0 products")
    return items

# ---------- storage ----------
def open_db(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, first_seen INTEGER, notified INTEGER, data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c

def meta_get(c, k, d=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone(); return r[0] if r else d

def meta_set(c, k, v):
    if meta_get(c, k) != str(v): c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))

# ---------- notifications ----------
def ntfy_send(topic, title, message, click=None, attach=None, tags=("shopping_bags",), tries=4):
    body = {"topic": topic, "title": title, "message": message, "tags": list(tags)}
    if click: body["click"] = click
    if attach: body["attach"] = attach
    for i in range(tries):
        try:
            req = urllib.request.Request(NTFY, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status == 200: return True
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(6); continue
        except Exception: pass
        time.sleep(2 ** (i + 1))
    return False

def enrich(it):
    """Best-effort: read price from the product page's JSON-LD (only called for steals that will alert)."""
    if it.get("price"): return it
    try:
        html = fetch(it["url"], 2)
        for blk in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
            d = json.loads(blk); d = d[0] if isinstance(d, list) else d
            off = d.get("offers") or {}; off = off[0] if isinstance(off, list) else off
            if off.get("price"): it = dict(it, price=int(float(off["price"]))); break
    except Exception: pass
    return it

def fmt(it):
    lines = ["Product: " + it["name"]]
    if it.get("price"): lines.append(f"Price: R{it['price']:,}")
    if it.get("was"): lines.append(f"Was: R{it['was']:,}")
    if it.get("pct"): lines.append(f"Discount: {it['pct']}%")
    if it.get("image"): lines.append("Image: " + it["image"])
    lines += ["", it["url"]]
    return "\n".join(lines)

# ---------- image badge detector (blue "VERIFIED ✓ STEAL" pill baked into the product image) ----------
BADGE_TPL = "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"
BADGE_BOX = (16, 296, 176, 318)   # pill position on a 328x328 normalised image
BADGE_NCC, BADGE_YELLOW = 0.5, 40  # match score and gold-seal pixel count thresholds
_tpl = None

def badge_score(data):
    """Returns (ncc, gold_pixels) for image bytes. Plain pixel maths (Pillow+numpy), no AI."""
    global _tpl
    import io, base64, numpy as np
    from PIL import Image
    if _tpl is None:
        _tpl = np.asarray(Image.open(io.BytesIO(base64.b64decode(BADGE_TPL))).convert("L"), float)
    im = Image.open(io.BytesIO(data)).resize((328, 328))
    a, rgb = np.asarray(im.convert("L"), float), np.asarray(im.convert("RGB"), int)
    h, w = _tpl.shape; tz = _tpl - _tpl.mean(); tn = np.sqrt((tz ** 2).sum()); best = (-1.0, 0, 0)
    for dy in range(-12, 13, 2):
        for dx in range(-12, 13, 2):
            y0, x0 = BADGE_BOX[1] + dy, BADGE_BOX[0] + dx
            if y0 < 0 or x0 < 0 or y0 + h > 328 or x0 + w > 328: continue
            win = a[y0:y0 + h, x0:x0 + w]; wz = win - win.mean(); d = np.sqrt((wz ** 2).sum()) * tn
            if d > 0 and (wz * tz).sum() / d > best[0]: best = ((wz * tz).sum() / d, y0, x0)
    sc, y0, x0 = best; win = rgb[y0:y0 + h, x0:x0 + w]
    gold = int(((win[..., 0] > 215) & (win[..., 1] > 160) & (win[..., 1] < 235) & (win[..., 2] < 90)).sum())
    return round(float(sc), 3), gold

def fetch_bytes(url, tries=2):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
                return r.read()
        except Exception: time.sleep(1)
    return None

def check_images(cand, cap=600):
    """cand: {product_id: image_url} -> set of ids whose image carries the badge."""
    try: import numpy, PIL  # noqa
    except ImportError: log("Pillow/numpy missing: image badge check skipped"); return set()
    from concurrent.futures import ThreadPoolExecutor
    def one(kv):
        d = fetch_bytes(kv[1])
        try: sc = badge_score(d) if d else None
        except Exception: sc = None
        return kv[0], sc
    hits, fails = set(), 0
    with ThreadPoolExecutor(8) as ex:
        for k, sc in ex.map(one, list(cand.items())[:cap]):
            if sc is None: fails += 1
            elif sc[0] >= BADGE_NCC and sc[1] >= BADGE_YELLOW: hits.add(k)
    if fails: log(f"image check: {fails} images could not be read")
    if hits: log(f"image badge found on {len(hits)} product(s)")
    return hits

# ---------- core ----------
def ikey(u): return (u or "").split("?")[0]
BURST = 12  # this many new IDs in one run = daily page swap, not a random drop
def run(c, items, notify, now=None, rollover=False, img_check=None):
    """items: full catalogue. notify(title,msg,click,attach)->bool. Returns count of notifications sent."""
    now = now or int(time.time())
    stored = {r[0]: json.loads(r[1]) for r in c.execute("SELECT id,data FROM products")}
    known = set(stored)
    last = int(meta_get(c, "last_count", 0))
    if last > 20 and len(items) < last * 0.5:
        raise RuntimeError(f"catalogue suspiciously small ({len(items)} vs {last}); treating as scrape failure")
    baseline = meta_get(c, "baseline") != "done"
    new = [k for k in items if k not in known]
    silent = baseline or rollover or len(new) >= BURST
    if img_check and not baseline:  # check images of NEW products (and products whose image file changed)
        cand = {k: it["image"] for k, it in items.items() if it.get("image") and not it.get("steal") and
                (k not in stored or (ikey(it["image"]) != ikey(stored[k].get("image")) and not stored[k].get("steal")))}
        for k in img_check(cand): items[k]["steal"] = True; items[k]["badge_img"] = True
    if silent and not baseline: log(f"midnight/bulk change: {len(new)} new products (only badge products alert)")
    for k in new:  # stored BEFORE sending => restart-safe. Only badge products are pending (notified=0)
        pend = items[k].get("steal") and not baseline
        c.execute("INSERT INTO products VALUES(?,?,?,?)", (k, now, 0 if pend else 1, json.dumps(items[k])))
    for k, it in items.items():  # existing product that newly becomes a steal -> alert once
        if k in stored and it.get("steal") and not stored[k].get("steal"):
            c.execute("UPDATE products SET notified=0, data=? WHERE id=?", (json.dumps(it), k))
        elif k in stored and not stored[k].get("steal") and ikey(it.get("image")) != ikey(stored[k].get("image")):
            c.execute("UPDATE products SET data=? WHERE id=?", (json.dumps(it), k))  # remember new image
    if baseline:
        meta_set(c, "baseline", "done"); log(f"baseline stored: {len(new)} products, no notifications")
    meta_set(c, "last_count", len(items)); c.commit()
    sent = 0
    if silent and not baseline and new and now - int(meta_get(c, "last_swap", 0)) > 6 * 3600:
        # one "grid refreshed" notice per daily cycle (guard: max once per 6h)
        if notify("🔄 ONEDAYONLY GRID REFRESHED", f"Today's Deals have changed: {len(new)} new products.\n\n{BASE}/", BASE + "/", None):
            meta_set(c, "last_swap", now); c.commit(); sent += 1
    for pid, data in c.execute("SELECT id,data FROM products WHERE notified=0 ORDER BY first_seen").fetchall():
        it = enrich(json.loads(data))
        if not notify(f"🔥 STEAL: {it['pct']}% OFF" if it.get("pct") else "🔥 STEAL", fmt(it), it["url"], it.get("image")): break
        c.execute("UPDATE products SET notified=1 WHERE id=?", (pid,)); c.commit(); sent += 1
    left = c.execute("SELECT COUNT(*) FROM products WHERE notified=0").fetchone()[0]
    if left: raise RuntimeError(f"{left} notifications still pending (ntfy unreachable?); will retry next run")
    log(f"catalogue={len(items)} new={len(new)} sent={sent}")
    return sent

def in_rollover_window():
    from zoneinfo import ZoneInfo; from datetime import datetime
    t = datetime.now(ZoneInfo("Africa/Johannesburg")); mins = t.hour * 60 + t.minute
    return mins >= 23 * 60 + 55 or mins <= 45  # 23:55-00:45 SAST (covers GitHub cron delays)

def discover():
    html = fetch(BASE + "/"); items, anc = parse_items(html)
    segs = {}
    for a in anc:
        u = norm(a["href"])
        if u: segs.setdefault(urllib.parse.urlsplit(u).path.strip("/").split("/")[0], []).append(a)
    print("anchors:", len(anc), "| candidate products on homepage:", len(items))
    print("JSON-LD present:", "application/ld+json" in html, "| __NEXT_DATA__:", "__NEXT_DATA__" in html)
    for s, v in sorted(segs.items(), key=lambda x: -len(x[1]))[:25]:
        print(f"  /{s}: {len(v)}  e.g. {norm(v[0]['href'])}  [{v[0]['text'][:40]}]")
    print("seeds:", find_seeds(anc)[:10])
    ms = list(STEAL.finditer(html))
    print("badge pattern in page HTML:", len(ms), "| steals (>=%d%% off) in Today's cards:" % MIN_STEAL_PCT, sum(1 for v in items.values() if v["steal"]))
    for m_ in ms[:2]: print("  badge context:", html[max(0, m_.start() - 150):m_.end() + 100].replace("\n", " "))
    for k, v in list(items.items())[:8]: print(" ", k, v)
    try:
        print("IMAGE BADGE SCAN (first 60 cards): id | ncc,gold | image url")
        for k, v in list(items.items())[:60]:
            d = fetch_bytes(v["image"]) if v.get("image") else None
            sc = badge_score(d) if d else None
            if sc and (sc[0] >= 0.3 or sc[1] >= 20): print("  ", k, sc, v["image"])
        for k, v in list(items.items())[:3]: print("  sample image:", v.get("image"))
    except Exception as e: print("image scan unavailable:", e)
    try:
        m_ = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S); nd = json.loads(m_.group(1))
        print("NEXT_DATA top keys:", list(nd)[:10], "| props keys:", list(nd.get("props", {}))[:10])
        lists, hits = [], []
        def walk(o, p):
            if isinstance(o, dict):
                for k, v in o.items():
                    if re.search(r"steal|verified|badge|label|tag", k, re.I) and len(hits) < 12: hits.append((p + "/" + k, str(v)[:80]))
                    walk(v, p + "/" + k)
            elif isinstance(o, list):
                if len(o) >= 20 and isinstance(o[0], dict): lists.append((p, len(o), o[0]))
                for i, v in enumerate(o[:3]): walk(v, p + f"[{i}]")
        walk(nd, "")
        for p, n, first in lists[:6]: print("LIST", p, n, "keys:", list(first)[:40], "\n   first:", json.dumps(first)[:700])
        for h in hits: print("FLAG-LIKE KEY", h)
    except Exception as e: print("NEXT_DATA parse failed:", e)
    try:
        ph = fetch(list(items)[0] and list(items.values())[0]["url"], 1)
        print("PRODUCT PAGE JSON-LD:", [b[:600] for b in re.findall(r'application/ld\+json[^>]*>(.*?)</script>', ph, re.S)][:2])
    except Exception as e: print("product page fetch failed:", e)
    for path in ("/robots.txt", "/sitemap.xml"):
        try: print(path, "->", fetch(BASE + path, 1)[:600].replace("\n", " | "))
        except Exception as e: print(path, "unavailable:", e)

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    topic = os.environ.get("NTFY_TOPIC", "")
    if arg == "--discover": return discover()
    if arg == "--test-notify":
        ok = ntfy_send(topic, "🛍️ ntfy test", "If you can read this, notifications work.")
        log("test notification", "sent" if ok else "FAILED"); sys.exit(0 if ok else 1)
    db = DB
    if arg == "--dry-run":  # real fetch, no notifications, DB untouched
        db = tempfile.mktemp(suffix=".db")
        if os.path.exists(DB): shutil.copy(DB, db)
    if arg != "--dry-run" and not topic: sys.exit("NTFY_TOPIC not set")
    notify = (lambda t, m, click, att: print("[dry-run]", t, "\n", m) or True) if arg == "--dry-run" else \
             (lambda t, m, click, att: ntfy_send(topic, t, m, click, att))
    loop = int(sys.argv[2]) if arg == "--loop" and len(sys.argv) > 2 else 0
    end, c, ok = time.time() + loop, open_db(db), True
    while True:
        try:
            run(c, crawl(fetch), notify, rollover=in_rollover_window(), img_check=check_images)
            meta_set(c, "fails", 0); meta_set(c, "down_alert", 0); c.commit(); ok = True
        except Exception as e:
            ok = False; n = int(meta_get(c, "fails", 0)) + 1; meta_set(c, "fails", n); c.commit(); log("ERROR:", e)
            if n == 10 and arg != "--dry-run" and meta_get(c, "down_alert") != "1":
                ntfy_send(topic, "\u26a0\ufe0f OneDayOnly monitor failing", f"10 failed checks in a row. Last error: {e}", tags=("warning",))
                meta_set(c, "down_alert", "1"); c.commit()
        if time.time() + 60 > end: break
        time.sleep(60)
    sys.exit(0 if ok else 1)

if __name__ == "__main__": main()
