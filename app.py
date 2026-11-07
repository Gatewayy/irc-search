#!/usr/bin/env python3
"""irclogs: private full-text search over Brett's IRC history, plus public shareable quotes.

Standard library only. Reads the FTS5 index read-only and keeps shared quotes in a
small writable sqlite file. Everything except /q/<id> is meant to sit behind
Cloudflare Access; /q/<id> pages are self-contained (inline CSS, no JS needed).

Env: HISTORY_DB (default /data/history.sqlite3), QUOTES_DB (/quotes/quotes.sqlite3),
     PORT (8080), TZ_NAME (America/Chicago), PUBLIC_BASE (https://irclogs.gatewayy.net)
"""
import hashlib, html, json, os, re, secrets, sqlite3, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

HISTORY_DB = os.environ.get("HISTORY_DB", "/data/history.sqlite3")
QUOTES_DB = os.environ.get("QUOTES_DB", "/quotes/quotes.sqlite3")
PORT = int(os.environ.get("PORT", "8080"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))
PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://irclogs.gatewayy.net").rstrip("/")
PAGE = 50
esc = html.escape

# ---------- data ----------
_local = threading.local()

def hist():
    c = getattr(_local, "h", None)
    if c is None:
        c = sqlite3.connect(f"file:{HISTORY_DB}?mode=ro", uri=True, timeout=30, check_same_thread=False)
        c.execute("pragma query_only=1")
        _local.h = c
    return c

def quotes():
    c = getattr(_local, "q", None)
    if c is None:
        c = sqlite3.connect(QUOTES_DB, timeout=30, check_same_thread=False)
        c.execute("""create table if not exists quotes (id text primary key, num integer, created integer,
                     title text, net text, chan text, masked integer, lines text)""")
        c.commit()
        _local.q = c
    return c

_channels = {"at": 0, "rows": []}
def channels():
    if time.time() - _channels["at"] > 3600 or not _channels["rows"]:
        rows = hist().execute("select net, chan, count(*), min(t), max(t) from m group by net, chan order by count(*) desc").fetchall()
        _channels.update(at=time.time(), rows=rows)
    return _channels["rows"]

def nets():
    return sorted({r[0] for r in channels()})

def fmt_t(t, with_date=True):
    d = datetime.fromtimestamp(t / 1000, TZ)
    return d.strftime("%b %-d, %Y · %-I:%M %p") if with_date else d.strftime("%-I:%M %p")

def nick_hue(nick):
    return int(hashlib.md5(nick.lower().rstrip("_`^-").encode()).hexdigest()[:4], 16) % 360

LINK = re.compile(r"(https?://[^\s<>\"']+)")
def render_text(text, marks=False):
    """Escape, optionally turn \x01..\x02 highlight markers into <mark>, linkify."""
    s = esc(text)
    if marks:
        s = s.replace("\x01", "<mark>").replace("\x02", "</mark>")
    return LINK.sub(lambda m: f'<a href="{m.group(1)}" rel="noopener noreferrer nofollow" target="_blank">{m.group(1)}</a>', s)

def fts_query(q):
    """Let FTS5 syntax through; if it fails to parse, fall back to quoting each word."""
    try:
        hist().execute("select count(*) from (select 1 from f where f match ? limit 1)", (q,)).fetchone()
        return q
    except sqlite3.OperationalError:
        return " ".join('"' + w.replace('"', '""') + '"' for w in q.split())

# ---------- html ----------
CSS = """
:root{--bg:#0f1216;--panel:#171b21;--panel2:#1d222a;--text:#e6e9ee;--muted:#9aa4b2;--line:#2a3039;
--accent:#7cc4ff;--mark:#ffd86b33;--markt:#ffe08a;--radius:12px;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--text:#14171c;--muted:#5b6573;
--line:#dde1e7;--accent:#0a63c9;--mark:#ffd86b66;--markt:#5a4300;color-scheme:light}}
*{box-sizing:border-box}html{font-size:17px}
body{margin:0;background:var(--bg);color:var(--text);font:1rem/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--accent)}header{display:flex;gap:1rem;align-items:center;padding:1rem 1.25rem;border-bottom:1px solid var(--line);flex-wrap:wrap}
header .brand{font-weight:700;font-size:1.15rem;text-decoration:none;color:var(--text)}header .brand span{color:var(--accent)}
header nav a{margin-right:1rem;color:var(--muted);text-decoration:none}header nav a:hover{color:var(--text)}
main{max-width:1100px;margin:0 auto;padding:1.25rem}
form.search{display:grid;grid-template-columns:1fr auto;gap:.6rem;margin-bottom:.8rem}
form.search .row2{grid-column:1/-1;display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;color:var(--muted);font-size:.9rem}
input,select,button,textarea{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.55rem .75rem}
input[type=search]{font-size:1.1rem}button{background:var(--accent);color:#04121f;border:0;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.hint{color:var(--muted);font-size:.85rem;margin:.2rem 0 1rem}code{background:var(--panel2);padding:.05rem .35rem;border-radius:6px}
.log{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
.ln{display:grid;grid-template-columns:auto 9.5rem 1fr;gap:.75rem;padding:.4rem .9rem;border-top:1px solid var(--line);align-items:baseline}
.ln:first-child{border-top:0}.ln.hit{background:var(--panel2)}.ln .ts{color:var(--muted);font-size:.82rem;white-space:nowrap;font-variant-numeric:tabular-nums}
.ln .who{font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ln .msg{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92rem;word-break:break-word;white-space:pre-wrap}
.ln .act{font-style:italic}.ln input{margin:0;width:1.05rem;height:1.05rem}
mark{background:var(--mark);color:var(--markt);border-radius:3px;padding:0 .1em}
.res{margin-bottom:.9rem}.res .meta{display:flex;justify-content:space-between;gap:1rem;color:var(--muted);font-size:.85rem;margin:0 .2rem .25rem}
.res .meta a{text-decoration:none}.pill{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:.05rem .55rem;font-size:.8rem;color:var(--muted)}
.bar{position:sticky;bottom:0;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:.8rem;margin-top:1rem;display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
table{width:100%;border-collapse:collapse}td,th{padding:.45rem .6rem;border-top:1px solid var(--line);text-align:left}th{color:var(--muted);font-weight:500}
td.n{text-align:right;font-variant-numeric:tabular-nums}.more{display:flex;justify-content:center;gap:1rem;margin:1rem 0}
.empty{color:var(--muted);padding:2rem;text-align:center}.qcard{max-width:820px;margin:2rem auto}
.qhead{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;margin-bottom:.6rem}.qhead h1{font-size:1.35rem;margin:0}
.qfoot{color:var(--muted);font-size:.85rem;margin-top:.7rem;display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap}
@media (max-width:640px){.ln{grid-template-columns:auto 1fr}.ln .who{text-align:left;grid-column:2}.ln .msg{grid-column:1/-1}}
"""

def page(title, body, private=True):
    nav = ('<nav><a href="/">Search</a><a href="/onthisday">On this day</a><a href="/random">Random</a><a href="/me">You on IRC</a><a href="/channels">Channels</a><a href="/quotes">Quotes</a><a href="/help">Help</a></nav>' if private else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="{'noindex,nofollow' if private else 'noindex'}"><title>{esc(title)}</title>
<style>{CSS}</style></head><body><header><a class="brand" href="{'/' if private else PUBLIC_BASE + '/'}">irc<span>logs</span></a>{nav}</header>
<main>{body}</main></body></html>"""

def who_html(nick, act=False):
    h = nick_hue(nick)
    style = f"color:hsl({h} 70% 62%)"
    return f'<span class="who" style="{style}">{"* " if act else ""}{esc(nick)}</span>'

def line_html(r, hit=False, checkbox=False, marks=False, with_date=False):
    _id, net, chan, t, nick, act, text = r[:7]
    cb = f'<input type="checkbox" name="ids" value="{_id}" aria-label="select line">' if checkbox else "<span></span>"
    msg = render_text(text, marks)
    return (f'<div class="ln{" hit" if hit else ""}" id="l{_id}">{cb}'
            f'<div>{who_html(nick, act)}<div class="ts">{esc(fmt_t(t, with_date))}</div></div>'
            f'<div class="msg{" act" if act else ""}">{msg}</div></div>')

# ---------- views ----------
def view_search(qs):
    q = qs.get("q", [""])[0].strip()
    net = qs.get("net", [""])[0]; chan = qs.get("chan", [""])[0].strip().lower()
    nick = qs.get("nick", [""])[0].strip(); order = qs.get("order", ["new"])[0]
    year = qs.get("year", [""])[0].strip(); off = max(0, int(qs.get("off", ["0"])[0] or 0))
    net_opts = "".join(f'<option{" selected" if n == net else ""}>{esc(n)}</option>' for n in nets())
    form = f"""<form class="search" method="get" action="/">
<input type="search" name="q" value="{esc(q)}" placeholder='Search 11+ years of IRC… e.g. osmium, "exact phrase", znc AND webadmin' autofocus>
<button type="submit">Search</button><div class="row2">
<select name="net"><option value="">All networks</option>{net_opts}</select>
<input name="chan" value="{esc(chan)}" placeholder="#channel" size="14"><input name="nick" value="{esc(nick)}" placeholder="nick" size="12">
<input name="year" value="{esc(year)}" placeholder="year" size="5" inputmode="numeric">
<select name="order"><option value="new"{" selected" if order == "new" else ""}>Newest first</option>
<option value="old"{" selected" if order == "old" else ""}>Oldest first</option></select></div></form>
<p class="hint">Supports <code>"phrases"</code>, <code>AND</code>/<code>OR</code>/<code>NOT</code>, <code>prefix*</code>, <code>NEAR(a b)</code>, <code>nick:Name</code>. <a href="/help">All search tricks →</a></p>"""
    if not (q or nick):
        total = sum(r[2] for r in channels())
        return page("irclogs", form + f'<div class="empty">{total:,} lines across {len(channels()):,} channels and {len(nets())} networks.</div>')
    where, args = [], []
    parts = []
    if q:
        parts.append("(" + fts_query(q) + ")")
    if nick:
        parts.append('nick:"' + nick.replace('"', '""') + '"')
    where.append("f match ?"); args.append(" AND ".join(parts))
    if net:
        where.append("m.net = ?"); args.append(net)
    if chan:
        where.append("m.chan = ?"); args.append(chan)
    if year.isdigit():
        y = int(year)
        a = int(datetime(y, 1, 1, tzinfo=TZ).timestamp() * 1000); b = int(datetime(y + 1, 1, 1, tzinfo=TZ).timestamp() * 1000)
        where.append("m.t >= ? and m.t < ?"); args += [a, b]
    sql = (f"select m.id, m.net, m.chan, m.t, m.nick, m.act, highlight(f, 0, char(1), char(2)) from f join m on m.id = f.rowid "
           f"where {' and '.join(where)} order by m.t {'asc' if order == 'old' else 'desc'} limit ? offset ?")
    t0 = time.time()
    try:
        rows = hist().execute(sql, args + [PAGE + 1, off]).fetchall()
    except sqlite3.OperationalError as e:
        return page("irclogs", form + f'<div class="empty">Query error: {esc(str(e))}</div>')
    more = len(rows) > PAGE; rows = rows[:PAGE]; ms = (time.time() - t0) * 1000
    out = [form, f'<p class="hint">{"No" if not rows else (str(off + 1) + "–" + str(off + len(rows)))} results · {ms:.0f} ms</p>']
    for r in rows:
        ctx = f"/ctx?id={r[0]}"
        out.append(f'<div class="res"><div class="meta"><span><span class="pill">{esc(r[1])}</span> '
                   f'<a href="/?{urlencode({"net": r[1], "chan": r[2]})}">{esc(r[2])}</a></span>'
                   f'<a href="{ctx}#l{r[0]}">{esc(fmt_t(r[3]))} · context ↗</a></div>'
                   f'<div class="log">{line_html(r, marks=True)}</div></div>')
    if not rows:
        out.append('<div class="empty">Nothing found. Try fewer words or a prefix like <code>linu*</code>.</div>')
    nav = []
    base = {k: v for k, v in dict(q=q, net=net, chan=chan, nick=nick, order=order, year=year).items() if v}
    if off:
        nav.append(f'<a href="/?{urlencode({**base, "off": max(0, off - PAGE)})}">← Previous</a>')
    if more:
        nav.append(f'<a href="/?{urlencode({**base, "off": off + PAGE})}">More →</a>')
    out.append(f'<div class="more">{"".join(nav)}</div>')
    return page(f"{q or nick} · irclogs", "".join(out))

def view_ctx(qs):
    try:
        _id = int(qs.get("id", ["0"])[0]); span = min(400, max(10, int(qs.get("n", ["40"])[0])))
    except ValueError:
        return None
    c = hist()
    r = c.execute("select id, net, chan, t from m where id = ?", (_id,)).fetchone()
    if not r:
        return None
    _, net, chan, t = r
    before = c.execute("select id, net, chan, t, nick, act, text from m where net=? and chan=? and (t < ? or (t = ? and id < ?)) "
                       "order by t desc, id desc limit ?", (net, chan, t, t, _id, span)).fetchall()[::-1]
    after = c.execute("select id, net, chan, t, nick, act, text from m where net=? and chan=? and (t > ? or (t = ? and id >= ?)) "
                      "order by t, id limit ?", (net, chan, t, t, _id, span + 1)).fetchall()
    rows = before + after
    day = None; lines = []
    for x in rows:
        d = datetime.fromtimestamp(x[3] / 1000, TZ).strftime("%A, %B %-d, %Y")
        if d != day:
            lines.append(f'<div class="ln" style="grid-template-columns:1fr"><strong>{esc(d)}</strong></div>'); day = d
        lines.append(line_html(x, hit=(x[0] == _id), checkbox=True))
    wider = f"/ctx?id={_id}&n={span * 2}#l{_id}"
    body = f"""<div class="qhead"><h1>{esc(chan)} <span class="pill">{esc(net)}</span></h1>
<a href="{wider}">Show more around this ↕</a></div>
<form method="post" action="/share"><div class="log">{''.join(lines)}</div>
<div class="bar"><strong>Share selected lines</strong>
<input name="title" placeholder="Title (optional)" maxlength="120" size="28">
<label><input type="checkbox" name="mask" value="1"> mask nicks</label>
<button type="submit">Create public quote</button>
<span class="hint" style="margin:0">Only the checked lines are copied into the public page.</span></div></form>"""
    return page(f"{chan} · {net} · irclogs", body)

def view_channels(qs):
    rows = channels(); net = qs.get("net", [""])[0]
    tr = []
    for n, ch, cnt, a, b in rows:
        if net and n != net:
            continue
        span = f"{datetime.fromtimestamp(a/1000, TZ).year}–{datetime.fromtimestamp(b/1000, TZ).year}"
        tr.append(f'<tr><td><span class="pill">{esc(n)}</span></td><td><a href="/?{urlencode({"net": n, "chan": ch, "q": ""})}&nick=">{esc(ch)}</a></td>'
                  f'<td class="n">{cnt:,}</td><td class="n">{span}</td></tr>')
    tabs = " ".join(f'<a href="/channels?net={esc(x)}">{esc(x)}</a>' for x in nets())
    return page("Channels · irclogs", f'<p class="hint"><a href="/channels">All</a> {tabs}</p><div class="log" style="padding:.3rem .6rem">'
                f'<table><tr><th>Network</th><th>Channel / query</th><th class="n">Lines</th><th class="n">Years</th></tr>{"".join(tr)}</table></div>')

# ---------- nostalgia ----------
ME = re.compile(r"^gatewayy", re.I)
BOTS = {"o3", "u", "x3", "stats", "heretic", "chanserv", "nickserv", "p", "x", "grok", "idlebot", "cableguy", "athena"}
MIN_LEN = 25

def interesting(nick, text):
    return nick.lower().rstrip("_`^-|") not in BOTS and len(text) >= MIN_LEN and not text.startswith(("[", "!", "."))

def random_line():
    c = hist()
    lo, hi = c.execute("select min(id), max(id) from m").fetchone()
    for _ in range(40):
        r = c.execute("select id, nick, text, act from m where id >= ? limit 30", (secrets.randbelow(hi - lo + 1) + lo,)).fetchall()
        good = [x for x in r if not x[3] and interesting(x[1], x[2])]
        if good:
            return good[secrets.randbelow(len(good))][0]
    return lo

def day_window(y, mo, d):
    a = datetime(y, mo, d, tzinfo=TZ)
    return int(a.timestamp() * 1000), int(a.timestamp() * 1000) + 86_400_000

def view_onthisday(qs):
    now = datetime.now(TZ)
    try:
        mo, d = int(qs.get("m", [now.month])[0]), int(qs.get("d", [now.day])[0]); datetime(2000, mo, d)
    except (ValueError, TypeError):
        mo, d = now.month, now.day
    c = hist()
    y0 = datetime.fromtimestamp(c.execute("select min(t) from m").fetchone()[0] / 1000, TZ).year
    blocks = []
    for y in range(now.year, y0 - 1, -1):
        try:
            a, b = day_window(y, mo, d)
        except ValueError:
            continue  # Feb 29
        n, mine = c.execute("select count(*), sum(nick like 'gatewayy%') from m where t >= ? and t < ?", (a, b)).fetchone()
        if not n:
            continue
        rows = c.execute("select id, net, chan, t, nick, act, text from m where t >= ? and t < ? order by random() limit 60", (a, b)).fetchall()
        mine_rows = [r for r in rows if ME.match(r[4]) and len(r[6]) >= 8]
        picks = (mine_rows[:2] + [r for r in rows if interesting(r[4], r[6]) and r not in mine_rows])[:4]
        picks.sort(key=lambda r: r[3])
        top = c.execute("select net, chan, count(*) n from m where t >= ? and t < ? group by net, chan order by n desc limit 3", (a, b)).fetchall()
        where = ", ".join(f'<a href="/?{urlencode({"net": t_[0], "chan": t_[1], "q": ""})}">{esc(t_[1])}</a> <span class="pill">{esc(t_[0])}</span>' for t_ in top)
        lines = "".join(f'<a href="/ctx?id={r[0]}#l{r[0]}" style="text-decoration:none;color:inherit">{line_html(r)}</a>' for r in picks)
        blocks.append(f'<div class="res"><div class="meta"><strong style="color:var(--text);font-size:1.05rem">{y} · {"today" if y == now.year else str(now.year - y) + " year" + ("s" if now.year - y != 1 else "") + " ago"}</strong>'
                      f'<span>{n:,} lines · {mine or 0:,} by you · {where}</span></div><div class="log">{lines}</div></div>')
    prev = datetime(2000, mo, d).toordinal(); nav = []
    for delta, label in ((-1, "← previous day"), (1, "next day →")):
        x = datetime.fromordinal(prev + delta)
        nav.append(f'<a href="/onthisday?m={x.month}&d={x.day}">{label}</a>')
    title = datetime(2000, mo, d).strftime("%B %-d")
    body = (f'<div class="qhead"><h1>On this day: {esc(title)}</h1><span>{" · ".join(nav)}</span></div>'
            f'<p class="hint">Random lines from {esc(title)} in every year with history. Click a line to open it in context (and share it).</p>'
            + ("".join(blocks) or '<div class="empty">Nothing logged on this date.</div>'))
    return page(f"On this day · {title} · irclogs", body)

_me = {"at": 0, "html": ""}
def me_stats():
    c = hist()
    mine = c.execute("select m.net, m.chan, m.t from f join m on m.id = f.rowid where f match 'nick:gatewayy*'").fetchall()
    ys, cs = {}, {}
    for net, chan, t in mine:
        d = datetime.fromtimestamp(t / 1000, TZ)
        e = ys.setdefault(d.year, [0, set()]); e[0] += 1; e[1].add(d.toordinal())
        cs[(net, chan)] = cs.get((net, chan), 0) + 1
    years = [(y, v[0], len(v[1])) for y, v in sorted(ys.items())]
    chans = sorted(((a, b, n) for (a, b), n in cs.items()), key=lambda x: -x[2])[:12]
    total = sum(r[1] for r in years); days = sum(r[2] for r in years); mx = max([r[1] for r in years] or [1])
    first = c.execute("select m.t, m.net, m.chan, m.text, m.id from f join m on m.id = f.rowid where f match 'nick:gatewayy*' order by m.t limit 1").fetchone()
    bars = "".join(f'<tr><td>{y}</td><td class="n">{n:,}</td><td class="n">{dd:,}</td><td style="width:55%"><div style="background:var(--accent);opacity:.75;height:.7rem;border-radius:4px;width:{n / mx * 100:.1f}%"></div></td></tr>'
                   for y, n, dd in years)
    ch = "".join(f'<tr><td><span class="pill">{esc(a)}</span></td><td><a href="/?{urlencode({"net": a, "chan": b, "nick": "Gatewayy"})}">{esc(b)}</a></td><td class="n">{n:,}</td></tr>' for a, b, n in chans)
    est = total * 8 / 3600  # ~8 s of typing/reading per line you sent, a rough feel only
    firsth = (f'<p>Your first line on record: <a href="/ctx?id={first[4]}#l{first[4]}">{esc(fmt_t(first[0]))} in {esc(first[2])} ({esc(first[1])})</a>: '
              f'<code>{esc(first[3][:140])}</code></p>' if first else "")
    return f"""<div class="qhead"><h1>You on IRC</h1><span class="pill">nick:Gatewayy*</span></div>
<p style="font-size:1.15rem"><strong>{total:,}</strong> lines from you across <strong>{days:,}</strong> days with at least one line,
roughly <strong>{est:,.0f} hours</strong> of just your own typing (at ~8 s a line, before all the lurking).</p>{firsth}
<div class="log" style="padding:.3rem .6rem;margin-bottom:1rem"><table><tr><th>Year</th><th class="n">Your lines</th><th class="n">Days</th><th></th></tr>{bars}</table></div>
<div class="log" style="padding:.3rem .6rem"><table><tr><th>Network</th><th>Where you talked most</th><th class="n">Lines</th></tr>{ch}</table></div>"""

def view_me(qs):
    if time.time() - _me["at"] > 6 * 3600 or not _me["html"]:
        _me.update(at=time.time(), html=me_stats())
    return page("You on IRC · irclogs", _me["html"])

def mask_map(lines):
    m = {}
    for l in lines:
        m.setdefault(l["nick"].lower(), f"user{len(m) + 1}")
    return m

def quote_lines_html(qrow):
    lines = json.loads(qrow[7]); masked = qrow[6]
    mm = mask_map(lines) if masked else {}
    out = []
    for l in lines:
        nick = mm.get(l["nick"].lower(), l["nick"]); text = l["text"]
        if masked:
            for real, fake in mm.items():
                text = re.sub(r"(?i)\b" + re.escape(real) + r"\b", fake, text)
        out.append(line_html((0, "", "", l["t"], nick, l["act"], text)))
    return "".join(out)

def view_quote(qid, raw=False):
    r = quotes().execute("select id, num, created, title, net, chan, masked, lines from quotes where id = ?", (qid,)).fetchone()
    if not r:
        return None
    if raw:
        lines = json.loads(r[7]); mm = mask_map(lines) if r[6] else {}
        txt = []
        for l in lines:
            nick = mm.get(l["nick"].lower(), l["nick"]); text = l["text"]
            for real, fake in mm.items():
                text = re.sub(r"(?i)\b" + re.escape(real) + r"\b", fake, text)
            txt.append(f"[{fmt_t(l['t'])}] " + (f"* {nick} {text}" if l["act"] else f"<{nick}> {text}"))
        return "\n".join(txt) + "\n"
    first = json.loads(r[7])[0]["t"]
    title = r[3] or f"Quote #{r[1]}"
    where = "" if r[6] else f'{esc(r[5])} on {esc(r[4])} · '
    body = f"""<div class="qcard"><div class="qhead"><h1>{esc(title)}</h1><span class="pill">#{r[1]}</span></div>
<div class="log">{quote_lines_html(r)}</div>
<div class="qfoot"><span>{where}{esc(fmt_t(first))}</span><span><a href="/q/{esc(r[0])}.txt">plain text</a></span></div></div>"""
    return page(f"{title} · irclogs", body, private=False)

def view_quotes(qs):
    rows = quotes().execute("select id, num, created, title, net, chan, masked, lines from quotes order by num desc").fetchall()
    tr = []
    for r in rows:
        n = len(json.loads(r[7]))
        tr.append(f'<tr><td class="n">#{r[1]}</td><td><a href="/q/{esc(r[0])}">{esc(r[3] or "(untitled)")}</a></td>'
                  f'<td><span class="pill">{esc(r[4])}</span> {esc(r[5])}</td><td class="n">{n}</td>'
                  f'<td>{"masked" if r[6] else ""}</td><td><form method="post" action="/quotes/delete" '
                  f'onsubmit="return confirm(\'Delete quote #{r[1]}? The public link stops working.\')">'
                  f'<input type="hidden" name="id" value="{esc(r[0])}"><button class="ghost">Delete</button></form></td></tr>')
    body = ('<div class="log" style="padding:.3rem .6rem"><table><tr><th class="n">#</th><th>Title</th><th>Where</th>'
            f'<th class="n">Lines</th><th></th><th></th></tr>{"".join(tr)}</table></div>' if rows
            else '<div class="empty">No quotes yet. Open any search result in context, tick some lines, and share.</div>')
    return page("Quotes · irclogs", f"<p class='hint'>Public pages live at <code>{esc(PUBLIC_BASE)}/q/&lt;id&gt;</code>. Everything else here is private.</p>{body}")

def do_share(form):
    ids = [int(i) for i in form.get("ids", []) if i.isdigit()][:200]
    if not ids:
        return None, "Select at least one line."
    qmarks = ",".join("?" * len(ids))
    rows = hist().execute(f"select id, net, chan, t, nick, act, text from m where id in ({qmarks}) order by t, id", ids).fetchall()
    if not rows:
        return None, "Those lines no longer exist."
    lines = [{"t": r[3], "nick": r[4], "act": r[5], "text": r[6]} for r in rows]
    qid = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
    qc = quotes()
    num = (qc.execute("select coalesce(max(num),0) from quotes").fetchone()[0] or 0) + 1
    qc.execute("insert into quotes values (?,?,?,?,?,?,?,?)", (qid, num, int(time.time()), form.get("title", [""])[0][:120].strip(),
               rows[0][1], rows[0][2], 1 if form.get("mask") else 0, json.dumps(lines, ensure_ascii=False)))
    qc.commit()
    return qid, None


HELP_HTML = """<div class="qhead"><h1>Search help</h1><span class="pill">irclogs</span></div>
<div class="log" style="padding:1rem 1.2rem;margin-bottom:1rem">
<h2 style="margin-top:0">The search box</h2>
<p>Case doesn't matter, accents are ignored, and words are matched whole (punctuation splits words, so <code>#linux</code> finds <code>linux</code>).</p>
<table>
<tr><th>Type this</th><th>Finds</th></tr>
<tr><td><code>znc webadmin</code></td><td>lines with <b>both</b> words, anywhere in the line (a space means AND)</td></tr>
<tr><td><code>"jupe andromeda"</code></td><td>the exact phrase, words next to each other in that order</td></tr>
<tr><td><code>znc OR bnc</code></td><td>either word (<code>OR</code>, <code>AND</code>, <code>NOT</code> must be capitals)</td></tr>
<tr><td><code>znc NOT webadmin</code></td><td>znc, but not lines that also say webadmin</td></tr>
<tr><td><code>linu*</code></td><td>anything starting with linu: linux, linuxmint… (prefix only; no <code>*ux</code>)</td></tr>
<tr><td><code>(znc OR bnc) AND osmium</code></td><td>group with parentheses</td></tr>
<tr><td><code>NEAR(osmium link, 5)</code></td><td>both words within 5 words of each other (default 10)</td></tr>
<tr><td><code>nick:Moose</code></td><td>lines <b>said by</b> Moose (also matches Moose_ / Moose`); combine: <code>nick:Gatewayy pizza</code></td></tr>
<tr><td><code>nick:gate*</code></td><td>nicks starting with gate</td></tr>
<tr><td><code>text:nick</code></td><td>the literal word "nick" in the message text (when a word looks like a column name)</td></tr>
</table>
<p class="hint">If the syntax doesn't parse (a stray quote or bracket), irclogs falls back to searching each word plainly.</p>
<h2>Filters under the box</h2>
<table>
<tr><td><b>Network</b></td><td>AfterNET, BTN, Libera, P2P, PTP, Ars, SynIRC, plus the 2002–03 ones: NetGamers, kralizec, UnderNet, Legion, Chaos-Networks, IRC-Unity, LITECODE.NET</td></tr>
<tr><td><b>#channel</b></td><td>exact channel or query name, e.g. <code>#linux</code>, <code>#afternet</code>, or a nick for a private query (<code>moose</code>). Combine with Network, since #linux exists on several.</td></tr>
<tr><td><b>nick</b></td><td>same as <code>nick:Name</code> in the box; leave the box empty to read everything someone said</td></tr>
<tr><td><b>year</b></td><td>one year, e.g. <code>2003</code></td></tr>
<tr><td><b>Order</b></td><td>newest first (default) or oldest first: oldest is great for "when did I first mention X?"</td></tr>
</table>
<p class="hint">Results come 50 at a time; use More → at the bottom. A channel name in a result links to that channel's newest lines.</p>
</div>
<div class="log" style="padding:1rem 1.2rem;margin-bottom:1rem">
<h2 style="margin-top:0">Recipes</h2>
<table>
<tr><td>Everything you said about Osmium, oldest first</td><td><code>osmium</code> + nick <code>Gatewayy</code> + Oldest first</td></tr>
<tr><td>The Andromeda saga</td><td><code>andromeda</code>, Network NetGamers, Oldest first</td></tr>
<tr><td>Your 2003 "u" era</td><td><code>u</code> + nick <code>Gatewayy</code> + year <code>2003</code></td></tr>
<tr><td>Links someone posted</td><td><code>https</code> + nick <code>Locke</code></td></tr>
<tr><td>Two people talking about something</td><td><code>(nick:Moose OR nick:Gatewayy) linking</code></td></tr>
<tr><td>A server or host</td><td><code>"osmium afternet org"</code> (dots split words, so use a phrase)</td></tr>
</table>
</div>
<div class="log" style="padding:1rem 1.2rem;margin-bottom:1rem">
<h2 style="margin-top:0">Other pages</h2>
<table>
<tr><td><a href="/onthisday">On this day</a></td><td>today's date in every year with history: counts, how many were yours, busiest channels, a few random lines. <code>?m=11&amp;d=7</code> picks a date; ← / → walk the calendar.</td></tr>
<tr><td><a href="/random">Random</a></td><td>jumps to a random real line (no bots, 25+ characters) in context. Reload for another.</td></tr>
<tr><td><a href="/me">You on IRC</a></td><td>your lines per year, days active, top channels, first line on record (refreshes every 6 h).</td></tr>
<tr><td><a href="/channels">Channels</a></td><td>every channel and query with line counts and years; filter by network.</td></tr>
<tr><td><a href="/quotes">Quotes</a></td><td>quotes you've shared, with delete.</td></tr>
</table>
</div>
<div class="log" style="padding:1rem 1.2rem;margin-bottom:1rem">
<h2 style="margin-top:0">Sharing a quote</h2>
<ol><li>Click <b>context ↗</b> on any result (or use Random / On this day).</li>
<li>Tick the lines you want. <b>Show more around this</b> widens the window.</li>
<li>Optional title; tick <b>mask nicks</b> to turn names into user1, user2… (also inside the text; the channel and network are hidden too).</li>
<li><b>Create public quote</b> → you land on <code>/q/&lt;id&gt;</code>. That URL works for anyone; <code>/q/&lt;id&gt;.txt</code> is plain text. Only the ticked lines are copied; nothing else here is public.</li></ol>
<p class="hint">Delete a quote in <a href="/quotes">Quotes</a> and its link stops working.</p>
</div>
<div class="log" style="padding:1rem 1.2rem">
<h2 style="margin-top:0">On the VPS (command line)</h2>
<p><code>irc-search 'query' [-n Network] [-c '#channel'] [-l 50] [--oldest] [--context 3]</code>: same query syntax as the box; <code>--context N</code> prints N lines around each hit.</p>
<p class="hint">What's in here: IRCCloud history 2015–Nov 2026 and the 2002–03 mIRC logs, plus The Lounge's new chat, synced nightly at 5:37 AM. Bot and status channels (#MrSnoopy, announce channels, idlerpg, *status) aren't indexed. Times are Central.</p>
</div>"""

def view_help(qs):
    return page("Help · irclogs", HELP_HTML)

# ---------- http ----------
class H(BaseHTTPRequestHandler):
    server_version = "irclogs"
    def log_message(self, fmt, *a):
        pass
    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def redirect(self, loc):
        self.send_response(303); self.send_header("Location", loc); self.send_header("Content-Length", "0"); self.end_headers()
    def do_GET(self):
        u = urlparse(self.path); qs = parse_qs(u.query)
        try:
            if u.path == "/healthz":
                hist().execute("select 1 from m limit 1"); return self.send(200, "ok\n", "text/plain")
            m = re.fullmatch(r"/q/([A-Za-z0-9]{4,16})(\.txt)?", u.path)
            if m:
                out = view_quote(m.group(1), raw=bool(m.group(2)))
                if out is None:
                    return self.send(404, page("Not found", '<div class="empty">This quote doesn\'t exist (or was removed).</div>', private=False))
                return self.send(200, out, "text/plain; charset=utf-8" if m.group(2) else "text/html; charset=utf-8",
                                 {"Cache-Control": "public, max-age=300"})
            if u.path == "/":
                return self.send(200, view_search(qs))
            if u.path == "/ctx":
                out = view_ctx(qs)
                return self.send(200, out) if out else self.send(404, page("Not found", '<div class="empty">Line not found.</div>'))
            if u.path == "/channels":
                return self.send(200, view_channels(qs))
            if u.path == "/onthisday":
                return self.send(200, view_onthisday(qs))
            if u.path == "/random":
                i = random_line(); return self.redirect(f"/ctx?id={i}#l{i}")
            if u.path == "/help":
                return self.send(200, view_help(qs))
            if u.path == "/me":
                return self.send(200, view_me(qs))
            if u.path == "/quotes":
                return self.send(200, view_quotes(qs))
            return self.send(404, page("Not found", '<div class="empty">Not found.</div>', private=False))
        except Exception as e:  # keep the server up; show a plain error
            return self.send(500, page("Error", f'<div class="empty">Error: {esc(type(e).__name__)}: {esc(str(e))}</div>'))
    def do_POST(self):
        u = urlparse(self.path)
        origin = self.headers.get("Origin") or ""
        host = self.headers.get("Host") or ""
        if origin and urlparse(origin).netloc not in (host, urlparse(PUBLIC_BASE).netloc):
            return self.send(403, "cross-origin post refused\n", "text/plain")
        n = min(int(self.headers.get("Content-Length") or 0), 200_000)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if u.path == "/share":
            qid, err = do_share(form)
            return self.redirect(f"/q/{qid}") if qid else self.send(400, page("Can't share", f'<div class="empty">{esc(err)}</div>'))
        if u.path == "/quotes/delete":
            quotes().execute("delete from quotes where id = ?", (form.get("id", [""])[0],)); quotes().commit()
            return self.redirect("/quotes")
        return self.send(404, "not found\n", "text/plain")

if __name__ == "__main__":
    os.makedirs(os.path.dirname(QUOTES_DB) or ".", exist_ok=True)
    quotes()
    print(f"irclogs on :{PORT} history={HISTORY_DB} quotes={QUOTES_DB}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
