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
PRICE = re.compile(r"R\s?(\d[\d,\s]*)")
SAVE = re.compile(r"save\s*-\s*(\d+)\s*%", re.I)
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
        super().__init__(); s.out = []; s.cur = None
    def handle_starttag(s, t, a):
        a = dict(a)
        if t == "a" and a.get("href"):
            s.cur = {"href": a["href"], "text": [], "img": None, "alt": None}
        elif t == "img" and s.cur is not None:
            src = a.get("data-src") or a.get("src") or ""
            if src and not s.cur["img"]: s.cur["img"] = src
            s.cur["alt"] = s.cur["alt"] or a.get("alt")
    def handle_data(s, d):
        if s.cur is not None and d.strip(): s.cur["text"].append(d.strip())
    def handle_endtag(s, t):
        if t == "a" and s.cur is not None:
            s.cur["text"] = " ".join(s.cur["text"]); s.out.append(s.cur); s.cur = None

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
        if path == "/" or EXCLUDE.match(path) or txt.lower() in SEED_TEXT: continue
        if not (a["img"] or PRICE.search(txt)): continue
        prices = [money(x) for x in PRICE.findall(txt)]
        sv = SAVE.search(txt)
        name = re.sub(r"(save\s*-\s*[\dR%,.]+|more options|R\s?\d[\d,\s]*)", "", txt, flags=re.I).strip(" -") or a["alt"] or url
        img = urllib.parse.urljoin(base, a["img"]) if a["img"] else None
        items.setdefault(product_id(url), {"url": url, "name": name[:150], "price": prices[0] if prices else None,
            "was": prices[1] if len(prices) > 1 else None, "pct": int(sv.group(1)) if sv else None, "image": img})
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
    """Today's Deals ONLY. Raises on ANY failure (never returns a partial/empty catalogue)."""
    ok = robots_ok(fetcher)
    _, anchors = parse_items(fetcher(BASE + "/"))
    seeds = [norm(a["href"]) for a in anchors if a["text"].lower() == "today's deals" and norm(a["href"])]
    if not seeds: raise RuntimeError("Today's Deals link not found on homepage")
    queue, seen, items = [seeds[0]], set(), {}
    while queue:
        u = queue.pop(0)
        if u in seen or not ok(urllib.parse.urlsplit(u).path): continue
        seen.add(u); time.sleep(delay)
        got, anc = parse_items(fetcher(u), u)
        items.update({k: v for k, v in got.items() if k not in items})
        for a in anc:  # pagination within Today's Deals
            nu = norm(a["href"], u)
            if nu and len(seen) < MAX_PAGES_PER_SEED and nu.split("?")[0] == u.split("?")[0] and nu not in seen and \
               ("page=" in a["href"] or a["text"].lower() in ("next", "\u00bb", "\u203a")):
                queue.append(nu)
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

def fmt(it):
    lines = ["Product: " + it["name"]]
    if it.get("price"): lines.append(f"Price: R{it['price']:,}")
    if it.get("was"): lines.append(f"Was: R{it['was']:,}")
    if it.get("pct"): lines.append(f"Discount: {it['pct']}%")
    if it.get("image"): lines.append("Image: " + it["image"])
    lines += ["", it["url"]]
    return "\n".join(lines)

# ---------- core ----------
BURST = 12  # this many new IDs in one run = daily page swap, not a random drop
def run(c, items, notify, now=None, rollover=False):
    """items: full catalogue. notify(title,msg,click,attach)->bool. Returns count of notifications sent."""
    now = now or int(time.time())
    known = {r[0] for r in c.execute("SELECT id FROM products")}
    last = int(meta_get(c, "last_count", 0))
    if last > 20 and len(items) < last * 0.5:
        raise RuntimeError(f"catalogue suspiciously small ({len(items)} vs {last}); treating as scrape failure")
    baseline = meta_get(c, "baseline") != "done"
    new = [k for k in items if k not in known]
    silent = baseline or rollover or len(new) >= BURST
    if silent and not baseline: log(f"midnight/bulk change: {len(new)} products stored silently")
    for k in new:  # new IDs are stored as pending (notified=0) BEFORE sending => restart-safe, no duplicates
        c.execute("INSERT INTO products VALUES(?,?,?,?)", (k, now, 1 if silent else 0, json.dumps(items[k])))
    if baseline:
        meta_set(c, "baseline", "done"); log(f"baseline stored: {len(new)} products, no notifications")
    meta_set(c, "last_count", len(items)); c.commit()
    sent = 0
    for pid, data in c.execute("SELECT id,data FROM products WHERE notified=0 ORDER BY first_seen").fetchall():
        it = json.loads(data)
        if not notify("🛍️ NEW ONEDAYONLY PRODUCT", fmt(it), it["url"], it.get("image")): break
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
    for k, v in list(items.items())[:8]: print(" ", k, v)
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
            run(c, crawl(fetch), notify, rollover=in_rollover_window())
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
