"""Hlidac verejnych zakazek: fotovoltaika (Praha + Stredocesky kraj).
Zdroje: Hlidac statu, NEN, Vestnik verejnych zakazek (VVZ), E-ZAK profily (ezak_profily.txt). Region zadavatele z ARES.
Bez AI, 0 tokenu. Jeden e-mail, jen nove zakazky."""
import json, os, re, smtplib, time
from datetime import datetime, date, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote, urljoin, urlencode
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEN = os.path.join(ROOT, "seen_fve.json")
ARES_CACHE = os.path.join(ROOT, "ares_kraje.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
HDR = {"User-Agent": UA, "Accept-Language": "cs-CZ,cs;q=0.9"}

# --- NASTAVENI FILTRU ------------------------------------------------------
TERMS = ["fotovoltaick", "FVE"]      # hledana slova (NEN a Tender Arena hledaji i zacatek slova)
TITLE_RE = re.compile(r"fotovolt|\bFVE\b", re.I)     # lokalni filtr (VVZ)
HLIDAC_QUERIES = ["fotovoltaick*", "FVE"]
HLIDAC_PAGES = 4
CORE = ["praha", "středočeský"]
USE_TENDER_ARENA = True   # Tender Arena: stovky profilů z Prahy a Středních Čech, čte se postupně (max 18 min na běh)
SWEEP = os.environ.get("TA_SWEEP") == "1"   # jednorazovy plny pruchod vsech profilu Tender Arena (workflow ta_pruchod)
FIRST_RUN_LIMIT = 30
KEY = re.compile(r"na\s+klíč|elektrárn|výrobn|FVE|fotovolt", re.I)
KOD_KRAJE = {19: "Hlavní město Praha", 27: "Středočeský kraj", 35: "Jihočeský kraj", 43: "Plzeňský kraj",
             51: "Karlovarský kraj", 60: "Ústecký kraj", 78: "Liberecký kraj",
             86: "Královéhradecký kraj", 94: "Pardubický kraj", 108: "Kraj Vysočina"}
VVZ_FORMS = ["10", "11", "12", "13", "14", "16", "17", "18", "19", "20", "21", "22", "F02", "F05",
             "F12", "F17", "F21", "F22", "F23", "F24", "CZ02", "CZ07"]
PRIORITY = {"NEN": 0, "E-ZAK": 1, "VVZ": 2, "Tender Arena": 3, "Tender Arena hledání": 3, "Hlídač státu": 4}
# ---------------------------------------------------------------------------

def clean(s): return re.sub(r"\s+", " ", s or "").strip()
def esc(t): return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def get(url, tries=3, timeout=90, **kw):
    last = None
    for n in range(tries):
        try:
            r = requests.get(url, headers=HDR, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(3 * (n + 1))
    raise last

def pdate(s):
    m = re.search(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(20\d\d)", s or "")
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    except ValueError:
        return None

def iso_date(s):
    try:
        return date.fromisoformat((s or "")[:10]) if s else None
    except ValueError:
        return None

def ms_date(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().date() if ms else None
    except Exception:
        return None

def tkey(title):
    t = re.sub(r"\s+—\s+.*$", "", (title or "").lower())
    return re.sub(r"[^a-z0-9ěščřžýáíéúůďťňó]+", "", t)[:30]

# ---------------------------------------------------------------- ARES (region)
def ares_cache_load():
    return json.load(open(ARES_CACHE, encoding="utf-8")) if os.path.exists(ARES_CACHE) else {}

def kraj_from(sidlo):
    s = sidlo or {}
    return s.get("nazevKraje") or KOD_KRAJE.get(s.get("kodKraje"), "")

ARES_BUDGET = 420          # max sekund celkem na dotazy do ARES
_ares_t0 = [None]

def ares_ok():
    if _ares_t0[0] is None:
        _ares_t0[0] = time.time()
    return time.time() - _ares_t0[0] < ARES_BUDGET

def kraj_ico(ico, cache):
    if not ico:
        return ""
    ico = str(ico).zfill(8)
    if ico in cache and cache[ico]:
        return cache[ico]
    if not ares_ok():
        return ""
    try:
        j = get(f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}", tries=1, timeout=15).json()
        k = kraj_from(j.get("sidlo"))
        cache[ico] = k
        time.sleep(0.1)
        return k
    except Exception:
        return ""

def kraj_name(name, cache):
    """Region podle nazvu zadavatele (kdyz neni ICO): ARES vyhledani podle obchodniho jmena."""
    if not name:
        return ""
    ck = "n:" + name.lower()
    if cache.get(ck):
        return cache[ck]
    if not ares_ok():
        return ""
    try:
        r = requests.post("https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/vyhledat",
                          json={"obchodniJmeno": name, "start": 0, "pocet": 10}, headers=HDR, timeout=15)
        r.raise_for_status()
        subj = r.json().get("ekonomickeSubjekty", [])
        exact = [s for s in subj if (s.get("obchodniJmeno") or "").strip().lower() == name.strip().lower()]
        pick = exact[0] if exact else (subj[0] if len(subj) == 1 else None)
        k = kraj_from(pick.get("sidlo")) if pick else ""
        if k:
            cache[ck] = k
        time.sleep(0.1)
        return k
    except Exception:
        return ""

KNOWN_REGION = {"čez, a. s.": "Hlavní město Praha", "čez, a.s.": "Hlavní město Praha"}

def region(k):
    kl = (k or "").lower()
    if any(x in kl for x in CORE):
        return "core"
    return "jine" if kl else "neznamy"

# ---------------------------------------------------------------- Hlidac statu
def hlidac_parse(html):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        c = [clean(td.get_text(" ")) for td in tds]
        m = re.match(r"(\S+)\s*(.*)", c[0])
        if not m:
            continue
        zid, stav = m.group(1), m.group(2)
        if zid in seen:
            continue
        seen.add(zid)
        a = tr.find("a", href=re.compile(r"/verejnezakazky/zakazka/"))
        href = urljoin("https://www.hlidacstatu.cz", a["href"].split("?")[0]) if a else ""
        ico = None
        for l in tds[3].find_all("a", href=True):
            mm = re.search(r"/subjekt/(\d{6,8})", l["href"])
            if mm:
                ico = mm.group(1).zfill(8)
        if not ico:
            mm = re.search(r"\((\d{6,8})\)", c[3])
            ico = mm.group(1).zfill(8) if mm else None
        if not stav.lower().startswith("zahájen"):
            continue
        out.append({"source": "Hlídač státu", "keys": [zid], "title": c[5], "zadavatel": re.sub(r"\(\d+\)", "", c[3]).strip(),
                    "ico": ico, "datum": pdate(c[1]), "lhuta": pdate(c[2]), "url": href, "cena": c[6] if len(c) > 6 else ""})
    return out

def fetch_hlidac():
    items = []
    for q in HLIDAC_QUERIES:
        for p in range(1, HLIDAC_PAGES + 1):
            url = "https://www.hlidacstatu.cz/verejnezakazky/hledat?Q=" + quote(q) + "&order=1" + (f"&page={p}" if p > 1 else "")
            html = get(url).text
            n_rows = len([1 for tr in BeautifulSoup(html, "html.parser").select("table tr") if len(tr.find_all("td")) >= 6])
            items += hlidac_parse(html)
            if n_rows < 50:
                break
    return items

# ---------------------------------------------------------------- browser fetch
BF_JS = """async ([jobs, conc]) => {
  const out = new Array(jobs.length); let i = 0;
  async function w() {
    while (true) {
      const k = i++; if (k >= jobs.length) return;
      const [url, method, body] = jobs[k];
      try {
        const r = await fetch(url, {method, headers: body ? {'Content-Type':'application/json','Accept':'application/json'} : {'Accept':'application/json'},
                                    body: body ? JSON.stringify(body) : undefined});
        out[k] = {status: r.status, text: await r.text()};
      } catch (e) { out[k] = {status: 0, text: String(e)}; }
    }
  }
  await Promise.all(Array.from({length: conc}, w));
  return out;
}"""

def bfetch(page, url, method="GET", body=None):
    res = page.evaluate(BF_JS, [[[url, method, body]], 1])[0]
    if res["status"] >= 400 or res["status"] == 0:
        raise RuntimeError(f"HTTP {res['status']} {url[:80]} :: {res['text'][:600]}")
    return json.loads(res["text"])

def bfetch_many(page, jobs, conc=4):
    """jobs = [(url, method, body)] -> [(status, data|None, text[:200])]; selhane (0/429/5xx) se zkusi znovu"""
    def run(js):
        out = []
        for i in range(0, len(js), 300):
            for r in page.evaluate(BF_JS, [js[i:i + 300], conc]):
                data = None
                if 200 <= r["status"] < 300:
                    try:
                        data = json.loads(r["text"])
                    except Exception:
                        pass
                out.append((r["status"], data, r["text"][:200]))
        return out
    res = run(jobs)
    bad = [i for i, r in enumerate(res) if r[0] == 0 or r[0] == 429 or r[0] >= 500]
    if bad:
        time.sleep(5)
        for i, r in zip(bad, run([jobs[i] for i in bad])):
            res[i] = r
    return res

def open_page(ctx, url):
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    time.sleep(3)
    return page

# ---------------------------------------------------------------- NEN
NEN_URL = "https://nen.nipez.cz/api/datarows?className=Zadavaci_postup_hist"
NEN_BASE_ATTRS = [{"clientName": "id", "serverName": "ID"}, {"clientName": "kod", "serverName": "Kod"},
                  {"clientName": "nazev", "serverName": "Nazev"}, {"clientName": "stavZP", "serverName": "Stav_ZP"},
                  {"clientName": "zadavatelNazev", "serverName": "Zadavatel.Nazev"},
                  {"clientName": "podaniLhuta", "serverName": "Podani_nabidka.Lhuta"}]
NEN_EXTRA = [[{"clientName": "zadavatelIco", "serverName": "Zadavatel.ICO"}, {"clientName": "vytvoreno", "serverName": "create_cas"}],
             [{"clientName": "vytvoreno", "serverName": "create_cas"}],
             [{"clientName": "zadavatelIco", "serverName": "Zadavatel.ICO"}],
             []]

def nen_map(r):
    if (r.get("stavZP") or "").lower() != "neukoncena":
        return None
    kod = r.get("kod") or ""
    return {"source": "NEN", "keys": ["nen:" + kod], "title": clean(r.get("nazev")), "zadavatel": clean(r.get("zadavatelNazev")),
            "ico": (str(r.get("zadavatelIco")).zfill(8) if r.get("zadavatelIco") else None),
            "datum": iso_date(r.get("vytvoreno")), "lhuta": iso_date(r.get("podaniLhuta")),
            "url": "https://nen.nipez.cz/verejne-zakazky/detail-zakazky/" + kod.replace("/", "-"), "cena": ""}

def fetch_nen_api(ctx):
    page = open_page(ctx, "https://nen.nipez.cz/verejne-zakazky")
    items, ids = [], set()
    try:
        for term in TERMS:
            rows_all, extra_ok, first_err = [], None, None
            start = 0
            for _ in range(12):
                got = None
                for extra in ([extra_ok] if extra_ok is not None else NEN_EXTRA):
                    body = {"className": "Zadavaci_postup_hist", "attributes": NEN_BASE_ATTRS + extra,
                            "condition": "(((((:DBNEN.FiltrovatZPNaLW(Fulltext_filtr, @1)))))) AND (Hierarchie = null and Posledni_uverejneni = true)",
                            "conditionParams": [term], "count": 50, "startIndex": start,
                            "orderBy": ["DESC create_cas"], "cacheCount": "long", "abortSignal": {}}
                    try:
                        got = bfetch(page, NEN_URL, "POST", body)
                        extra_ok = extra
                        break
                    except Exception as e:
                        if first_err is None:
                            first_err = e
                        last_err = e
                if got is None:
                    raise RuntimeError(f"{first_err} | zaklad: {last_err}")
                rows_all += got
                if len(got) < 50:
                    break
                start += 50
            for r in rows_all:
                it = nen_map(r)
                if it and it["keys"][0] not in ids:
                    ids.add(it["keys"][0])
                    items.append(it)
    finally:
        page.close()
    return items

def fetch_nen_ui(ctx, notes):
    """Zaloha: do hledaciho pole NEN napise slovo (jako clovek) a precte odpovedi, ktere si aplikace sama vyzada."""
    items, ids = [], set()
    for term in TERMS:
        page = ctx.new_page()
        got = []
        def on(r, got=got, term=term):
            try:
                if r.request.method == "POST" and "/api/datarows?" in r.url and "Zadavaci_postup_hist" in r.url:
                    pd = json.loads(r.request.post_data or "{}")
                    if pd.get("conditionParams") == [term] and pd.get("count", 0) >= 10 and r.status == 200:
                        got.extend(r.json())
            except Exception:
                pass
        page.on("response", on)
        try:
            page.goto("https://nen.nipez.cz/verejne-zakazky", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(3)
        try:
            cand = page.locator("input[type=search], input[type=text], input:not([type])")
            for i in range(min(cand.count(), 6)):
                el = cand.nth(i)
                if el.is_visible():
                    el.fill(term)
                    el.press("Enter")
                    break
        except Exception as e:
            notes.append(f"NEN (prohlížeč): hledací pole nenalezeno ({str(e)[:100]}).")
        time.sleep(7)
        for _ in range(10):
            before = len(got)
            try:
                page.mouse.wheel(0, 30000)
            except Exception:
                pass
            time.sleep(3)
            if len(got) == before:
                break
        page.close()
        for r in got:
            it = nen_map(r)
            if it and it["keys"][0] not in ids:
                ids.add(it["keys"][0])
                items.append(it)
    return items

def fetch_nen(ctx, notes):
    items = []
    try:
        items = fetch_nen_ui(ctx, notes)
    except Exception as e:
        notes.append(f"NEN (prohlížeč): {str(e)[:200]}")
    if not items:
        try:
            return fetch_nen_api(ctx)
        except Exception as e:
            notes.append(f"NEN: nic nenačteno, přímé volání API selhalo ({str(e)[:300]}).")
    return items

# ---------------------------------------------------------------- VVZ
def vvz_map(row):
    d = row.get("data") or {}
    zad = (d.get("zadavatele") or [{}])[0]
    ev = d.get("evCisloZakazkyVvz") or ""
    return {"source": "VVZ", "keys": ["vvz:" + ev], "title": clean(d.get("nazevZakazky")),
            "zadavatel": clean(zad.get("nazev")), "ico": (str(zad.get("ico")).zfill(8) if zad.get("ico") else None),
            "datum": iso_date(d.get("datumUverejneniVvz")), "lhuta": iso_date(d.get("lhutaNabidkyZadosti")),
            "url": "https://vvz.nipez.cz/formulare-zakazky/" + ev, "cena": ""}

def fetch_vvz(ctx):
    page = open_page(ctx, "https://vvz.nipez.cz/")
    items, ids = [], set()
    try:
        today = date.today().isoformat()
        for pg in range(1, 200):
            params = [("formGroup", "vz"), ("form", "vz"), ("page", pg), ("limit", 50),
                      ("workflowPlace", "UVEREJNENO_VVZ"), ("data.formularZneplatnen", "false"), ("data.zakazkaZrusena", "false"),
                      ("data.lhutaNabidkyZadosti[gte]", today + "T00:00:00+00:00"),
                      ("order[data.lhutaNabidkyZadosti]", "ASC")]
            params += [(f"data.druhFormulare[{i}]", v) for i, v in enumerate(VVZ_FORMS)]
            rows = bfetch(page, "https://api.vvz.nipez.cz/api/submissions/search?" + urlencode(params))
            if not rows:
                break
            for r in rows:
                it = vvz_map(r)
                if TITLE_RE.search(it["title"]) and it["keys"][0] not in ids:
                    ids.add(it["keys"][0])
                    items.append(it)
    finally:
        page.close()
    return items


# ---------------------------------------------------------------- E-ZAK profily
EZAK_FILE = os.path.join(ROOT, "ezak_profily.txt")
EZAK_DEFAULT = """
Praha 4|https://ezak.praha4.cz/contract_index.html
Praha 5|https://zakazky.praha5.cz/contract_index.html
Praha 6|https://zakazky.praha6.cz/contract_index.html
Praha 8|https://ezak.praha8.cz/contract_index.html
Praha 12|https://zakazky.praha12.cz/contract_index.html
Praha 14|https://ezak.praha14.cz/contract_index.html
Praha 17|https://zakazky.repy.cz/contract_index.html
Praha 18 Letnany|https://zakazky.letnany.cz/contract_index.html
Praha 20|https://zakazky.pocernice.cz/contract_index.html
Stredocesky kraj|https://zakazky.kr-stredocesky.cz/contract_index.html
Beroun|https://zakazky.mesto-beroun.cz/contract_index.html
Benesov|https://ezak.benesov-city.cz/contract_index.html
Kutna Hora|https://zakazky.kutnahora.cz/contract_index.html
Nymburk|https://zakazky.mesto-nymburk.cz/contract_index.html
Horovice|https://zakazky.horovice.net/contract_index.html
Milovice|https://zakazky.mesto-milovice.cz/contract_index.html
Kolin|https://zakazky.mukolin.cz/contract_index.html
VAK Mlada Boleslav|https://zakazky.vakmb.info/contract_index.html
Operator ICT|https://zakazky.operatorict.cz/contract_index.html
Prazska vodohospodarska spolecnost|https://zakazky.pvs.cz/contract_index.html
CEPS|https://ztraty.ceps.cz/contract_index.html
CEPRO|https://zakazky.ceproas.cz/contract_index.html
VODA Zelivka|https://zakazky.vodazelivka.cz/contract_index.html
Ministerstvo financi|https://mfcr.ezak.cz/contract_index.html
MPSV|https://mpsv.ezak.cz/contract_index.html
MZP|https://ezak.mzp.cz/contract_index.html
MZe|https://zakazky.eagri.cz/contract_index.html
Sprava zeleznic|https://zakazky.spravazeleznic.cz/contract_index.html
Urad vlady|https://zakazky.vlada.cz/contract_index.html
SZIF|https://zakazky.szif.cz/contract_index.html
SSHR|https://zakazky.sshr.cz/contract_index.html
SPU|https://zakazky.spucr.cz/contract_index.html
SVS|https://zakazky.svscr.cz/contract_index.html
Urad pro civilni letectvi|https://zakazky.caa.cz/contract_index.html
CENDIS|https://zakazky.cendis.cz/contract_index.html
CNB|https://ezak.cnb.cz/contract_index.html
Cesky rozhlas|https://verejnezakazky.rozhlas.cz/contract_index.html
CSU|https://ezak-czso.cz/contract_index.html
CZU Praha|https://zakazky.czu.cz/contract_index.html
VSE Praha|https://zakazky.vse.cz/contract_index.html
Univerzita Karlova|https://zakazky.cuni.cz/contract_index.html
Vezenska sluzba|https://ezak.vscr.cz/contract_index.html
VOP CZ|https://verejnezakazky.vop.cz/contract_index.html
Ustav jaderne fyziky Rez|https://ezak.ujf.cas.cz/contract_index.html
CESNET|https://zakazky.cesnet.cz/contract_index.html
"""
EZAK_PAGES = 3
FINISHED = re.compile(r"zadáno|zadán|uzavřeno|zrušen|vyhodnoceno|objednáno|dokončen", re.I)
def _parse_ezak_raw(page_html, base_url, source):
    """E-ZAK: kazda zakazka je <tbody> se 2 radky: 1) nazev (odkaz), 2) rezim | faze | datum zahajeni | lhuta."""
    soup = BeautifulSoup(page_html, "html.parser")
    items = []
    for a in soup.select('a[href*="contract_display"]'):
        tr = a.find_parent("tr")
        if not tr:
            continue
        title = clean(a.get_text(" "))
        cells, sub = [], ""
        sib = tr.find_next_sibling("tr")
        while sib is not None:
            if sib.find("a", href=re.compile("contract_display")):
                break  # zacina dalsi zakazka
            tds = sib.find_all("td")
            if len(tds) >= 4:
                cells = [clean(c.get_text(" ")) for c in tds]
                break
            if tds and "sub-name" in (tds[0].get("class") or []):
                sub = clean(tds[0].get_text(" "))
            sib = sib.find_next_sibling("tr")
        if not title:
            continue
        stav = cells[1] if len(cells) > 1 else ""
        url = urljoin(base_url, a["href"])
        items.append({
            "source": (source + " – " + sub) if sub and sub != source else source,
            "title": title, "url": url, "key": url,
            "stav": stav,
            "datum": cells[2] if len(cells) > 2 else "",
            "lhuta": cells[3] if len(cells) > 3 else "",
            "open": not FINISHED.search(stav),
            "approx": False,
        })
    return items



def ezak_profiles():
    """Vestaveny seznam + (volitelne) dalsi radky ze souboru ezak_profily.txt."""
    lines = EZAK_DEFAULT.splitlines()
    if os.path.exists(EZAK_FILE):
        lines += open(EZAK_FILE, encoding="utf-8").read().splitlines()
    out, seen_u = [], set()
    for ln in lines:
        ln = ln.strip()
        if ln and not ln.startswith("#") and "|" in ln:
            n, u = (x.strip() for x in ln.split("|", 1))
            if u not in seen_u:
                seen_u.add(u)
                out.append((n, u))
    return out

EZAK_OPEN = re.compile(r"příjem|výzva k podání|projevení|prokazování|v jednání", re.I)
EZAK_DEADLINE = [0.0]

def fetch_ezak_one(prof):
    name, url = prof
    items = []
    for pg in range(1, EZAK_PAGES + 1):
        if time.time() > EZAK_DEADLINE[0]:
            break
        u = url if pg == 1 else url + ("&" if "?" in url else "?") + f"page={pg}"
        html = get(u, tries=2, timeout=20).text
        got = _parse_ezak_raw(html, url, name)
        if not got:
            break
        items += got
    out, seen_u = [], set()
    for g in items:
        if g["url"] in seen_u:
            continue
        seen_u.add(g["url"])
        if not TITLE_RE.search(g["title"]):
            continue
        # jen faze, kdy se jeste daji podat nabidky / zajem (ne Hodnoceni, Zadavani, Zadano, Vyhodnoceno...)
        if not EZAK_OPEN.search(g.get("stav", "")):
            continue
        lh = pdate(g.get("lhuta", ""))
        if lh and lh < date.today():
            continue
        dt = pdate(g.get("datum", ""))
        if not lh and dt and (date.today() - dt).days > 45:
            continue   # bez lhuty a starsi 45 dni = neaktualni (napr. stara konzultace)
        out.append({"source": "E-ZAK", "keys": ["ezak:" + g["url"]], "title": g["title"], "zadavatel": name,
                    "ico": None, "datum": pdate(g.get("datum", "")), "lhuta": lh, "url": g["url"], "cena": "",
                    "kraj_fixed": "Praha / Středočeský kraj"})
    return len(items), out

def fetch_ezak(notes):
    from concurrent.futures import ThreadPoolExecutor
    profs = ezak_profiles()
    EZAK_DEADLINE[0] = time.time() + 200
    if not profs:
        notes.append("E-ZAK: seznam profilů ezak_profily.txt nenalezen.")
        return []
    def safe(p):
        if time.time() > EZAK_DEADLINE[0]:
            return p, (0, []), "čas"
        try:
            return p, fetch_ezak_one(p), None
        except Exception as e:
            return p, (0, []), str(e)[:80]
    res, bad, rep = [], [], []
    with ThreadPoolExecutor(5) as ex:
        for p, (n, its), err in ex.map(safe, profs):
            if err:
                bad.append(p[0])
                rep.append(f"{p[0]}: ✗")
            else:
                rep.append(f"{p[0]}: {n}" + (f" (FVE {len(its)})" if its else ""))
            res += its
    ok = len(profs) - len(bad)
    total_read = sum(int(r.split(": ")[1].split(" ")[0]) for r in rep if not r.endswith("✗"))
    zero = [r.split(":")[0] for r in rep if r.endswith(": 0")]
    line = f"E-ZAK: přečteno {ok} z {len(profs)} profilů, celkem {total_read} zakázek."
    if zero:
        line += f" Prázdné profily: {', '.join(zero)}."
    notes.append(line)
    if bad:
        txt = f"E-ZAK: nenačetlo se {len(bad)} z {len(profs)} profilů ({', '.join(bad[:8])})"
        notes.append(txt + (" – selhal větší počet" if len(bad) > len(profs) // 3 else ""))
    return res

# ---------------------------------------------------------------- Tender Arena
TA_API = "https://api.tenderarena.cz/ta/profil/"
TA_CACHE = os.path.join(ROOT, "ta_profily.json")
TA_HDR = {"User-Agent": UA, "Origin": "https://tenderarena.cz", "Referer": "https://tenderarena.cz/",
          "Accept": "application/json", "Content-Type": "application/json"}

def ta_map(r, ico=""):
    if (r.get("stav") or "").upper() != "NEUKONCENA":
        return None
    sc = r.get("systemoveCislo") or str(r.get("id"))
    return {"source": "Tender Arena", "keys": ["ta:" + sc], "title": clean(r.get("nazev")),
            "zadavatel": clean(r.get("uredniNazevZadavatele")), "ico": ico or None, "datum": None,
            "lhuta": ms_date(r.get("lhutaProPodaniNabidek")), "ta_id": r.get("idZadavatele"),
            "url": "https://tenderarena.cz/dodavatel/zakazka/" + str(r.get("id")), "cena": ""}

class TA:
    """Prenos pro Tender Arena API: nejdriv prime HTTP (rychle), jinak z prohlizece."""
    def __init__(self, ctx):
        self.ctx, self.page, self.mode = ctx, None, None
        self.deadline = time.time() + (19800 if SWEEP else 1080)     # bezny beh max 18 min; plny pruchod max 330 min
        self.pause_until = 0.0
        try:
            r = requests.post(TA_API + "seznam-profilu/", headers=TA_HDR, timeout=30,
                              json={"strankovani": {"pocetZaznamu": 1, "stranka": 1}, "filtr": {}, "razeni": {"atribut": "PRIORITA", "typRazeni": "SESTUPNE"}})
            if r.status_code == 200 and r.json().get("polozky") is not None:
                self.mode = "requests"
        except Exception:
            pass
        if not self.mode:
            self.page = open_page(ctx, "https://tenderarena.cz/")
            self.mode = "prohlížeč"

    def _one(self, job):
        url, method, body = job
        last = (0, None, "")
        for n in range(6):
            if time.time() > self.deadline:
                return (0, None, "časový limit")
            wait = self.pause_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 60))
            try:
                r = requests.request(method, url, headers=TA_HDR, json=body, timeout=40) if body is not None else requests.request(method, url, headers=TA_HDR, timeout=40)
                data = None
                if 200 <= r.status_code < 300:
                    try:
                        data = r.json()
                    except Exception:
                        pass
                last = (r.status_code, data, r.text[:200])
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After", "")
                    w = int(ra) if ra.isdigit() else 15 * (n + 1)
                    self.pause_until = max(self.pause_until, time.time() + min(w, 90))
                    continue
                if r.status_code < 500:
                    time.sleep(0.15)
                    return last
            except Exception as e:
                last = (0, None, str(e)[:200])
            time.sleep(2 * (n + 1))
        return last

    def paced(self, job, st):
        """Jeden dotaz s adaptivnim tempem: pri HTTP 429 zpomali a pocka. st = dict(delay, n429, ok, fail)."""
        url, method, body = job
        for n in range(5):
            if time.time() > self.deadline:
                return (0, None, "časový limit")
            time.sleep(st["delay"])
            if self.mode != "requests":
                r = bfetch_many(self.page, [job], 1)[0]
                status, data, txt = r
            else:
                try:
                    resp = requests.request(method, url, headers=TA_HDR, json=body, timeout=40)
                    status, txt = resp.status_code, resp.text[:200]
                    data = resp.json() if 200 <= status < 300 else None
                except Exception as e:
                    status, data, txt = 0, None, str(e)[:200]
            if status == 429:
                st["n429"] += 1
                st["delay"] = min(st["delay"] * 1.6, 6.0)
                time.sleep(20)
                continue
            if data is not None:
                st["ok"] += 1
                if st["ok"] % 25 == 0:
                    st["delay"] = max(0.35, st["delay"] * 0.9)
                return (status, data, txt)
            if status >= 500 or status == 0:
                time.sleep(2)
                continue
            return (status, data, txt)
        st["fail"] += 1
        return (status, None, txt)

    def many(self, jobs):
        if self.mode == "requests":
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(3) as ex:
                return list(ex.map(self._one, jobs))
        if time.time() > self.deadline:
            return [(0, None, "časový limit")] * len(jobs)
        return bfetch_many(self.page, jobs)

    def call(self, url, method="GET", body=None):
        st, data, txt = self.many([(url, method, body)])[0]
        if data is None:
            raise RuntimeError(f"HTTP {st} {url[-40:]} :: {txt[:200]}")
        return data

    def close(self):
        if self.page:
            self.page.close()

def ta_body(skip, filtr):
    return {"strankovani": {"strankovaciTokenZaznamu": None, "preskocitZaznamu": skip, "hledatPred": False, "pocetZaznamu": 30},
            "filtr": filtr, "razeni": {"atribut": "LHUTA_PRO_DORUCENI_NABIDEK", "typRazeni": "SESTUPNE"}}

def fetch_ta(ctx, cache, notes):
    t = TA(ctx)
    try:
        notes.append(f"Tender Arena: přenos dat – {t.mode}.")
        return fetch_ta_profiles(t, cache, notes)
    finally:
        t.close()

def fetch_ta_profiles(t, cache, notes):
    """Spolecne hledani Tender Arena neumi -> seznam profilu, filtr podle PSC/regionu, hledani v kazdem profilu."""
    tc = json.load(open(TA_CACHE, encoding="utf-8")) if os.path.exists(TA_CACHE) else {}
    profs, pg = [], 1
    while pg < 100:
        j = t.call(TA_API + "seznam-profilu/", "POST",
                   {"strankovani": {"pocetZaznamu": 100, "stranka": pg}, "filtr": {}, "razeni": {"atribut": "PRIORITA", "typRazeni": "SESTUPNE"}})
        got = j.get("polozky", [])
        profs += got
        if len(got) < 100:
            break
        pg += 1
    todo = [p for p in profs if p["id"] not in tc]
    for p, (st, d, _t) in zip(todo, t.many([(TA_API + "detail-profilu/" + p["id"], "GET", None) for p in todo])):
        if d:
            tc[p["id"]] = {"ic": (d.get("identifikacniUdaje") or {}).get("ic") or p.get("ic"),
                           "psc": ((d.get("adresa") or {}).get("psc") or "").replace(" ", ""),
                           "ident": (d.get("profil") or {}).get("identifikator") or ""}
    json.dump(tc, open(TA_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    # Praha = PSC 1xxxx, Stredocesky kraj = PSC 2xxxx (zadne ARES dotazy -> rychle)
    core = [(pid, v) for pid, v in tc.items() if v.get("ident") and (v.get("psc", "")[:1] in ("1", "2") or not v.get("psc"))]
    # aktivita profilu: aktivni = mel zakazku s lhutou za poslednich 12 mesicu; spici profily se kontroluji jen jednou za 60 dni
    now_ts = time.time()
    def is_active(v):
        return bool(v.get("act")) and v["act"] >= now_ts - 365 * 86400
    n_all = len(core)
    n_unchecked = sum(1 for _, v in core if not v.get("chk"))
    n_active = sum(1 for _, v in core if v.get("chk") and is_active(v))
    n_sleep = sum(1 for _, v in core if v.get("chk") and not is_active(v))
    core = [(pid, v) for pid, v in core if not v.get("chk") or is_active(v) or now_ts - v["chk"] > 60 * 86400]
    core.sort(key=lambda x: x[1].get("chk", 0))          # nejdriv ty, ktere se dlouho nekontrolovaly
    st = {"delay": 0.6, "n429": 0, "ok": 0, "fail": 0}
    items, ids, n_fve, big, checked = [], set(), 0, [], 0
    for pid, v in core:
        if time.time() > t.deadline:
            break
        stt, res, txt = t.paced((TA_API + "seznam-zakazek/", "POST", ta_body(0, {"identifikatorProfilu": v["ident"]})), st)
        if res is None:
            continue
        v["chk"] = time.time()
        checked += 1
        if checked % 100 == 0:
            json.dump(tc, open(TA_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        rows = res.get("polozky", [])
        lh = [r["lhutaProPodaniNabidek"] for r in rows if r.get("lhutaProPodaniNabidek")]
        v["act"] = max(lh) / 1000 if lh else 0
        if len(rows) >= 30:
            big.append(v)
        for r in rows:
            if TITLE_RE.search(r.get("nazev") or ""):
                n_fve += 1
                it = ta_map(r, v["ic"])
                if it and it["keys"][0] not in ids:
                    ids.add(it["keys"][0])
                    items.append(it)
    for v in big:                                          # velke profily: hledat jeste podle slova
        for term in TERMS:
            if time.time() > t.deadline:
                break
            stt, res, txt = t.paced((TA_API + "seznam-zakazek/", "POST", ta_body(0, {"nazev": term, "identifikatorProfilu": v["ident"]})), st)
            for r in (res or {}).get("polozky", []):
                n_fve += 1
                it = ta_map(r, v["ic"])
                if it and it["keys"][0] not in ids:
                    ids.add(it["keys"][0])
                    items.append(it)
    json.dump(tc, open(TA_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    oldest = min((v.get("chk", 0) for _, v in core), default=0)
    stale = "ještě nikdy" if oldest == 0 else f"před {(time.time() - oldest) / 86400:.1f} dny"
    notes.append(f"Tender Arena: profilů z Prahy a Středních Čech {n_all} (dosud nezkontrolováno {n_unchecked}, aktivních {n_active}, spících bez zakázky za 12 měsíců {n_sleep}).")
    notes.append(f"Tender Arena: zkontrolováno {checked} z {len(core)} profilů z Prahy a Středních Čech (nejdéle nekontrolovaný: {stale}); "
                 f"omezení rychlosti (429): {st['n429']}×, konečné tempo {st['delay']:.1f} s/dotaz.")
    if checked < len(core):
        notes.append(f"Tender Arena: zkontrolováno jen {checked} z {len(core)} profilů (časový limit) – zbytek se dočte při příštím spuštění.")
    notes.append(f"Tender Arena: nalezeno {n_fve} zakázek s FVE, z toho otevřených {len(items)}.")
    return items


def fetch_ta_search(ctx, notes):
    """Celoplosne hledani na Tender Arena (Chytre vyhledavani): stranka vola vlastni API, odpovedi odchytime a zapiseme diagnostiku."""
    from collections import Counter
    page = ctx.new_page()
    cap = []
    def on_resp(r):
        try:
            if "api.tenderarena.cz" in r.url and r.request.method in ("POST", "GET"):
                ct = (r.headers or {}).get("content-type", "")
                if "json" in ct:
                    cap.append((r.url, r.request.post_data or "", r.json()))
        except Exception:
            pass
    page.on("response", on_resp)
    items, ids = [], set()
    try:
        page.goto("https://tenderarena.cz/dodavatel/chytre-vyhledavani", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        for term in ("fotovolt", "FVE"):
            n0 = len(cap)
            box = page.locator("input[type=search], input[type=text], input:not([type])").first
            box.fill(term)
            box.press("Enter")
            time.sleep(8)
            for u, post, j in cap[n0:]:
                rows = j.get("polozky") if isinstance(j, dict) else None
                if not rows or not isinstance(rows[0], dict):
                    continue
                path = u.split("?")[0].replace("https://api.tenderarena.cz/", "")
                stavy = dict(Counter(str(r.get("stav")) for r in rows))
                nz = [clean(r.get("nazev") or "")[:60] for r in rows[:4]]
                keys = ",".join(list(rows[0].keys())[:12])
                tot = j.get("celkovyPocetZaznamu")
                notes.append(f"Tender Arena hledání '{term}' → {path}: {len(rows)} řádků (celkem {tot}), stavy {stavy}; "
                             f"požadavek: {post[:350]}; pole: {keys}; ukázka: {nz}")
                for r in rows:
                    if r.get("nazev") and TITLE_RE.search(r["nazev"]):
                        it = ta_map(r, "")
                        if it and it["keys"][0] not in ids:
                            ids.add(it["keys"][0])
                            items.append(it)
        if not cap:
            notes.append("Tender Arena hledání: stránka nevolala žádné API.")
        notes.append(f"Tender Arena hledání: {len(items)} otevřených zakázek s FVE.")
    finally:
        page.close()
    return items

# ---------------------------------------------------------------- spolecne
def section(title, rows, note=""):
    h = [f"<h2>{esc(title)}</h2>"]
    if note:
        h.append(f"<p><i>{esc(note)}</i></p>")
    if not rows:
        h.append("<p>Nic nového.</p>")
        return "\n".join(h)
    h.append("<ol>")
    for r in rows:
        star = "⭐ " if KEY.search(r["title"]) else ""
        lh = f" · lhůta: <b>{r['lhuta']:%-d. %-m. %Y}</b>" if r.get("lhuta") else ""
        dt = f" · {r['datum']:%-d. %-m. %Y}" if r.get("datum") else ""
        kr = f" · {esc(r['kraj'])}" if r.get("kraj") else ""
        cena = f" · cena: {esc(r['cena'])}" if r.get("cena") else ""
        src = " + ".join(r["sources"])
        h.append(f'<li>{star}<a href="{r["url"]}">{esc(r["title"])}</a><br>'
                 f'<small>{esc(r["zadavatel"])}{kr}{lh}{cena}{dt} · <b>{esc(src)}</b></small></li>')
    h.append("</ol>")
    return "\n".join(h)

def send_mail(subject, body):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("MAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())

def merge(items):
    best = {}
    for it in items:
        k = tkey(it["title"])
        if not k:
            k = it["keys"][0]
        if k not in best:
            it["sources"] = [it["source"]]
            it["tk"] = k
            best[k] = it
            continue
        a = best[k]
        keep, other = (a, it) if PRIORITY[a["source"]] <= PRIORITY[it["source"]] else (it, a)
        keep["keys"] = list(dict.fromkeys(keep["keys"] + other["keys"]))
        keep["sources"] = list(dict.fromkeys(a["sources"] + [it["source"]]))
        for f in ("datum", "lhuta", "ico", "cena"):
            if not keep.get(f) and other.get(f):
                keep[f] = other[f]
        keep["tk"] = k
        best[k] = keep
    return list(best.values())

def main():
    from playwright.sync_api import sync_playwright
    first = not os.path.exists(SEEN)
    seen = set(json.load(open(SEEN, encoding="utf-8"))) if not first else set()
    cache = ares_cache_load()
    errors, notes, counts, raw = [], [], {}, []

    import signal
    BUDGET = {"Hlídač státu": 360, "NEN": 480, "VVZ": 480, "E-ZAK": 360, "Tender Arena": 20400 if SWEEP else 1260, "Tender Arena hledání": 150}

    def _alarm(sig, frm):
        raise TimeoutError("překročen časový limit zdroje")

    def run(name, fn):
        # kazdy zdroj ma svuj casovy limit: kdyz se zasekne, e-mail se presto odesle s ostatnimi
        try:
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(BUDGET.get(name, 600))
            got = fn()
            counts[name] = len(got)
            raw.extend(got)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            counts[name] = 0
            errors.append(f"{name}: {str(e)[:900] or 'chyba'}")
        finally:
            signal.alarm(0)

    if not SWEEP:
        run("Hlídač státu", fetch_hlidac)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="cs-CZ", viewport={"width": 1440, "height": 900})
        if not SWEEP:
            run("NEN", lambda: fetch_nen(ctx, notes))
            run("VVZ", lambda: fetch_vvz(ctx))
            run("E-ZAK", lambda: fetch_ezak(notes))
        if USE_TENDER_ARENA:
            def _ta():
                try:
                    return fetch_ta(ctx, cache, notes)
                except Exception as e:
                    notes.append(f"Tender Arena: tentokrát nenačteno, zkusí se příště ({str(e)[:150]})")
                    return []
            def _tas():
                try:
                    return fetch_ta_search(ctx, notes)
                except BaseException as e:
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    notes.append(f"Tender Arena hledání: nepodařilo se ({str(e)[:120]})")
                    return []
            run("Tender Arena hledání", _tas)
            run("Tender Arena", _ta)
        browser.close()

    today = date.today()
    cand = [i for i in raw if not (i.get("lhuta") and i["lhuta"] < today)]
    items = merge(cand)
    for it in items:
        k = it.get("kraj_fixed") or (kraj_ico(it.get("ico"), cache) if it.get("ico") else "")
        if not k:
            k = KNOWN_REGION.get((it.get("zadavatel") or "").strip().lower(), "")
        if not k:
            k = kraj_name(it.get("zadavatel"), cache)
        it["kraj"], it["reg"] = k, region(k)
    core = [i for i in items if i["reg"] == "core"]
    unknown = [i for i in items if i["reg"] == "neznamy"]
    isnew = lambda i: not (set(i["keys"]) & seen or ("t:" + i["tk"]) in seen)
    key = lambda x: (x.get("datum") or date.min, PRIORITY[x["sources"][0]] * -1, x["keys"][0])
    new_core = sorted([i for i in core if isnew(i)], key=key, reverse=True)
    new_unknown = sorted([i for i in unknown if isnew(i)], key=key, reverse=True)
    total = len(new_core) + len(new_unknown)
    shown_core, shown_unknown = (new_core[:FIRST_RUN_LIMIT], new_unknown[:FIRST_RUN_LIMIT]) if first else (new_core, new_unknown)

    parts = []
    if first:
        parts.append("<p><b>První spuštění</b> – ukázka nejnovějších otevřených zakázek. Od příště jen nové.</p>")
    parts.append(section("Praha a Středočeský kraj", shown_core))
    if shown_unknown:
        parts.append(section("Region zadavatele se nepodařilo zjistit", shown_unknown, "Zkontrolujte ručně, zda patří do Prahy nebo Středních Čech."))
    cnt = " · ".join(f"{k}: {v}" for k, v in counts.items())
    parts.append(f"<p><small>Načteno otevřených zakázek podle zdroje – {cnt}.</small></p>")
    warn_kw = ("selhal", "limit", "nic nenačteno", "nenalezen", "(prohlížeč):")
    problems = errors + [n for n in notes if any(k in n for k in warn_kw) and not n.startswith("Tender Arena")]
    infos = [n for n in notes if n not in problems]
    if problems:
        parts.append("<h3>⚠ Pozor</h3><ul>" + "".join(f"<li>{esc(e)}</li>" for e in problems) + "</ul>")
    if infos:
        parts.append("<p><small>" + "<br>".join(esc(n) for n in infos) + "</small></p>")
    subj = f"FVE zakázky: {total} nových" if total else "FVE zakázky: nic nového"
    if SWEEP:
        subj = "Průchod Tender Arena dokončen – " + subj
    if errors:
        subj += " (⚠)"
    if SWEEP and not os.environ.get("GMAIL_USER"):
        # verejny repozitar bez hesel: jen ulozit pamet profilu (ta_profily.json), e-mail se neposila
        print("PRUCHOD HOTOV (bez e-mailu):", counts, notes)
        return
    send_mail(subj, "\n<hr>\n".join(parts))
    for i in core + unknown:
        seen |= set(i["keys"]) | {"t:" + i["tk"]}
    json.dump(sorted(seen), open(SEEN, "w", encoding="utf-8"))
    json.dump(cache, open(ARES_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    print("hotovo", total, counts, errors, notes)

if __name__ == "__main__":
    main()
