import os
import re
import io
import json
import base64
import socket
import ipaddress
import zipfile
from urllib.parse import urljoin, urlparse, quote
from threading import Lock

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template_string, Response, abort

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-animeverse-secret")
HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; AnimeVerseReader/1.0; +https://example.invalid)"
})
HTTP_TIMEOUT = 18
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 18 * 1024 * 1024
MAX_CHAPTER_PAGES = 250
lock = Lock()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE = os.environ.get("GITHUB_FILE", "data/anime_links.json")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-this-admin-password")
LOCAL_DB = "anime_links.json"

# ---------- Safe URL / fetch helpers ----------

def is_mangatown_url(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and (
            host == "mangatown.com" or host.endswith(".mangatown.com")
        )
    except Exception:
        return False

def safe_public_url(url):
    """Allow public HTTP(S) URLs only; block localhost/private-network targets."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        host = p.hostname.lower()
        if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
            return False
        try:
            addresses = socket.getaddrinfo(host, None)
            for item in addresses:
                ip = ipaddress.ip_address(item[4][0])
                if not ip.is_global:
                    return False
        except Exception:
            return False
        return True
    except Exception:
        return False

def fetch_html(url, mangatown_only=False):
    if mangatown_only and not is_mangatown_url(url):
        raise ValueError("Please use a MangaTown URL.")
    if not safe_public_url(url):
        raise ValueError("That URL is not a publicly reachable website.")
    r = HTTP.get(url, timeout=HTTP_TIMEOUT, stream=True, allow_redirects=True)
    r.raise_for_status()
    if mangatown_only and not is_mangatown_url(r.url):
        raise ValueError("MangaTown redirected to a different website. The request was stopped.")
    data = bytearray()
    for chunk in r.iter_content(65536):
        data.extend(chunk)
        if len(data) > MAX_HTML_BYTES:
            raise ValueError("The page is too large to scan.")
    return r.url, data.decode(r.encoding or "utf-8", errors="replace")

def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()

def abs_http_url(base, value):
    if not value:
        return ""
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    result = urljoin(base, value)
    p = urlparse(result)
    if p.scheme not in ("http", "https"):
        return ""
    return result

def parse_series(url):
    final_url, html = fetch_html(url, mangatown_only=True)
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".title-top, h1, .detail_info h1, .detail_info .title")
    title = clean_text(title_el.get_text(" ", strip=True)) if title_el else "MangaTown Series"
    cover_el = soup.select_one(".detail_info img, .detail_info .cover img, .manga_detail img")
    cover = ""
    if cover_el:
        cover = abs_http_url(final_url, cover_el.get("data-src") or cover_el.get("src") or "")
    chapters = []
    seen = set()
    selectors = [
        ".chapter_content .chapter_list a",
        ".chapter_content a",
        "#chapter_list a",
        ".chapter-list a",
        ".chapter_list a"
    ]
    for selector in selectors:
        for a in soup.select(selector):
            href = abs_http_url(final_url, a.get("href"))
            label = clean_text(a.get_text(" ", strip=True))
            if href and is_mangatown_url(href) and href not in seen and label:
                seen.add(href)
                chapters.append({"title": label, "url": href})
        if chapters:
            break
    if not chapters:
        # Conservative fallback: only chapter-looking links within the series path.
        base_path = urlparse(final_url).path.rstrip("/") + "/"
        for a in soup.select("a[href]"):
            href = abs_http_url(final_url, a.get("href"))
            label = clean_text(a.get_text(" ", strip=True))
            if not href or not is_mangatown_url(href) or not label:
                continue
            path = urlparse(href).path
            if path.startswith(base_path) and re.search(r"(chapter|ch[-_/]|\bc\d)", path, re.I):
                if href not in seen:
                    seen.add(href)
                    chapters.append({"title": label, "url": href})
    return {"title": title, "url": final_url, "cover": cover, "chapters": chapters}

def extract_total_pages(html):
    patterns = [
        r"\btotal_pages\s*[:=]\s*['\"]?(\d+)",
        r"\btotalPages\s*[:=]\s*['\"]?(\d+)",
        r"\btotal_page\s*[:=]\s*['\"]?(\d+)"
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            try:
                return max(1, min(int(m.group(1)), MAX_CHAPTER_PAGES))
            except Exception:
                pass
    return 0

def parse_reader_page(url):
    final_url, html = fetch_html(url, mangatown_only=True)
    soup = BeautifulSoup(html, "html.parser")
    images = []
    seen = set()
    selectors = [
        "#image",
        "#viewer .image",
        "#viewer img",
        ".reader-main img",
        ".chapter-reader img",
        ".manga-page img"
    ]
    for selector in selectors:
        for img in soup.select(selector):
            src = img.get("data-src") or img.get("data-original") or img.get("data-lazy-src") or img.get("src")
            src = abs_http_url(final_url, src)
            if src and src not in seen:
                seen.add(src)
                images.append(src)
        if images:
            break

    # Some MangaTown layouts expose total_pages and serve one image per numbered HTML page.
    total = extract_total_pages(html)
    if not images and total:
        base = final_url
        if not base.endswith("/"):
            base += "/"
        images = [base + ("" if i == 1 else f"{i}.html") for i in range(1, total + 1)]
    return {"url": final_url, "images": images[:MAX_CHAPTER_PAGES], "total": total or len(images)}

# ---------- Anime links storage (GitHub Contents API with local fallback) ----------

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

def github_file_url():
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{quote(GITHUB_FILE, safe='/')}"

def load_anime_items():
    if GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO:
        try:
            r = HTTP.get(github_file_url(), headers=github_headers(),
                         params={"ref": GITHUB_BRANCH}, timeout=12)
            if r.status_code == 200:
                payload = r.json()
                raw = base64.b64decode(payload.get("content", "")).decode("utf-8")
                data = json.loads(raw)
                return data if isinstance(data, list) else []
            if r.status_code == 404:
                return []
        except Exception:
            pass
    try:
        with open(LOCAL_DB, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def save_anime_items(items):
    encoded_text = json.dumps(items, ensure_ascii=False, indent=2)
    if GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO:
        url = github_file_url()
        sha = None
        get = HTTP.get(url, headers=github_headers(),
                       params={"ref": GITHUB_BRANCH}, timeout=12)
        if get.status_code == 200:
            sha = get.json().get("sha")
        elif get.status_code != 404:
            get.raise_for_status()
        payload = {
            "message": "Update AnimeVerse anime links",
            "content": base64.b64encode(encoded_text.encode("utf-8")).decode("ascii"),
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha
        put = HTTP.put(url, headers=github_headers(), json=payload, timeout=15)
        put.raise_for_status()
        return "github"
    with open(LOCAL_DB, "w", encoding="utf-8") as f:
        f.write(encoded_text)
    return "local"

# ---------- Frontend ----------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AnimeVerse</title>
<style>
:root{--bg:#090d18;--panel:#111a2b;--panel2:#172238;--line:#263650;--text:#edf3ff;--muted:#91a2bd;--a:#8b7cff;--b:#42d6bd;--danger:#ff7189}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0%,#1c2342 0,transparent 35%),var(--bg);color:var(--text);font:15px/1.5 Inter,Segoe UI,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:#0b1120ed;backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:15px max(20px,calc((100vw - 1200px)/2));display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.brand{font-size:21px;font-weight:900;letter-spacing:.3px}.brand span{color:var(--a)}nav{display:flex;gap:8px;flex-wrap:wrap}button,.tab{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:11px;padding:10px 14px;cursor:pointer;font-weight:700}button:hover,.tab.active{border-color:var(--a);background:#29264b}.wrap{max-width:1200px;margin:28px auto;padding:0 20px}.hero{padding:26px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(120deg,#171d36,#101a2b);margin-bottom:22px}.hero h1{margin:0 0 7px;font-size:clamp(25px,4vw,38px)}.muted{color:var(--muted)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:220px}input,textarea,select{width:100%;padding:12px 13px;border:1px solid var(--line);background:#0b1322;color:var(--text);border-radius:10px;font:inherit}textarea{min-height:75px;resize:vertical}.field{margin:12px 0}.field label{display:block;font-weight:700;margin-bottom:6px}.primary{background:linear-gradient(120deg,#7768ef,#9a70ed);border:0}.success{background:#123c3a;border-color:#286c65}.danger{color:#ff9aab}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.item{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;min-width:0}.item a{color:#b8b0ff;text-decoration:none}.item a:hover{text-decoration:underline}.small{font-size:13px;color:var(--muted)}.cover{width:100%;height:190px;object-fit:cover;border-radius:10px;background:#202b40}.hidden{display:none!important}.notice{padding:12px;border-radius:10px;background:#1b2a40;color:#c8d8f2;margin:12px 0}.check{display:flex;gap:10px;align-items:flex-start;color:#d3def1}.check input{width:auto;margin-top:5px}.chapter{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}.readerbar{position:sticky;top:65px;z-index:3;background:#0b1120ed;padding:12px;border:1px solid var(--line);border-radius:12px;display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}.readerimg{display:block;max-width:100%;height:auto;margin:14px auto;border-radius:5px;background:#101827}.readerstage{text-align:center;background:#050912;border-radius:14px;padding:10px;min-height:300px}.pill{font-size:12px;border:1px solid var(--line);padding:4px 8px;border-radius:20px;color:var(--muted)}footer{text-align:center;color:var(--muted);padding:35px 10px}
</style>
</head>
<body>
<header><div class="brand">ANIME<span>VERSE</span></div><nav>
<button class="tab active" data-tab="manga">Manga reader</button>
<button class="tab" data-tab="anime">Anime library</button>
<button class="tab" data-tab="admin">Manage anime</button>
</nav></header>
<main class="wrap">
<section id="manga" class="section">
<div class="hero"><h1>MangaTown Reader</h1><div class="muted">Load a MangaTown series, choose a chapter, and read supported pages here.</div></div>
<div class="panel">
<div class="field"><label>MangaTown series URL</label><input id="seriesUrl" placeholder="https://www.mangatown.com/manga/series_name/"></div>
<label class="check"><input type="checkbox" id="rights" checked><span>I confirm I have permission to view and download this manga content, and that accessing it complies with the site's terms.</span></label>
<div class="row" style="margin-top:14px"><button class="primary" onclick="loadSeries()">Scan series</button><span class="small" id="seriesStatus">Enter a MangaTown series URL to begin.</span></div>
</div>
<div id="seriesInfo" class="panel hidden"></div>
<div id="chapterList" class="panel hidden"></div>
<div id="reader" class="panel hidden">
<div class="readerbar"><div><b id="readerTitle">Reader</b><div class="small" id="pageCount"></div></div><div class="row"><button onclick="changePage(-1)">← Previous</button><button onclick="changePage(1)">Next →</button><button class="success" onclick="downloadChapter()">Download chapter ZIP</button></div></div>
<div class="readerstage" id="readerStage"><div class="muted">Chapter pages will appear here.</div></div>
</div>
</section>
<section id="anime" class="section hidden">
<div class="hero"><h1>Anime library</h1><div class="muted">Your manually added anime links, separate from MangaTown.</div></div>
<div class="row"><input class="grow" id="animeSearch" placeholder="Filter anime..." oninput="renderAnime()"><span class="pill" id="animeCount">0 titles</span></div>
<div id="animeGrid" class="grid" style="margin-top:15px"></div>
</section>
<section id="admin" class="section hidden">
<div class="hero"><h1>Manage anime links</h1><div class="muted">Add links to videos, episodes, or official anime pages.</div></div>
<div class="panel">
<div class="field"><label>Admin password</label><input id="adminPassword" type="password" placeholder="Your ADMIN_PASSWORD"></div>
<div class="field"><label>Anime title</label><input id="animeTitle" placeholder="e.g. Example Anime"></div>
<div class="field"><label>Link URL</label><input id="animeUrl" placeholder="https://..."></div>
<div class="field"><label>Link type</label><select id="animeType"><option value="video">Video / stream</option><option value="episode">Episode page</option><option value="official">Official website</option><option value="other">Other link</option></select></div>
<div class="field"><label>Cover image URL (optional)</label><input id="animeCover" placeholder="https://.../cover.jpg"></div>
<div class="field"><label>Notes (optional)</label><textarea id="animeNotes" placeholder="Season, episode, language, etc."></textarea></div>
<button class="primary" onclick="addAnime()">Save anime link</button><span class="small" id="adminStatus" style="margin-left:10px"></span>
</div><div class="notice">If GitHub settings are configured, links are saved to your repository's <code>data/anime_links.json</code>. Otherwise, the app uses a local JSON file.</div>
</section>
</main><footer>AnimeVerse · Flask edition</footer>
<script>
let currentSeries=null,currentImages=[],pageIndex=0,currentChapter=null,animeItems=[];
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-tab]").forEach(x=>x.classList.toggle("active",x===b));document.querySelectorAll(".section").forEach(x=>x.classList.toggle("hidden",x.id!==b.dataset.tab));if(b.dataset.tab==="anime")loadAnime();});
async function api(url,opts={}){let r=await fetch(url,opts);let d=await r.json().catch(()=>({error:"Unexpected server response"}));if(!r.ok)throw new Error(d.error||"Request failed");return d}
async function loadSeries(){const url=document.getElementById("seriesUrl").value.trim();const status=document.getElementById("seriesStatus");if(!document.getElementById("rights").checked){status.textContent="Please confirm you have permission first.";return}status.textContent="Scanning MangaTown series…";try{const d=await api("/api/manga/series",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,authorized:true})});currentSeries=d;document.getElementById("seriesInfo").classList.remove("hidden");document.getElementById("seriesInfo").innerHTML=`<h2>${esc(d.title)}</h2><div class="small"><a target="_blank" rel="noopener" href="${esc(d.url)}">Open original MangaTown series</a> · ${d.chapters.length} chapters found</div>`;const list=document.getElementById("chapterList");list.classList.remove("hidden");list.innerHTML="<h3>Chapters</h3>"+(d.chapters.length?d.chapters.map((c,i)=>`<div class="chapter"><span>${esc(c.title)}</span><button onclick="openChapter(${i})">Read</button></div>`).join(""):"<p class='muted'>No chapter links were found. MangaTown may have changed its page layout.</p>");status.textContent="Series scan complete."}catch(e){status.textContent=e.message}}
async function openChapter(i){if(!document.getElementById("rights").checked){alert("Confirm you have permission to view this content.");return}const c=currentSeries.chapters[i];const box=document.getElementById("reader");box.classList.remove("hidden");document.getElementById("readerTitle").textContent=currentSeries.title+" · "+c.title;document.getElementById("readerStage").innerHTML="<p class='muted'>Scanning chapter pages…</p>";try{const d=await api("/api/manga/chapter",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:c.url,authorized:true})});currentImages=d.images||[];pageIndex=0;currentChapter={title:c.title,url:c.url};showPage();box.scrollIntoView({behavior:"smooth"})}catch(e){document.getElementById("readerStage").textContent=e.message}}
function showPage(){const stage=document.getElementById("readerStage");if(!currentImages.length){stage.innerHTML="<p class='muted'>No page images were detected. Try opening the chapter on MangaTown to check whether it is available.</p>";document.getElementById("pageCount").textContent="0 pages";return}const src=currentImages[pageIndex];stage.innerHTML=`<img class="readerimg" src="${esc(src)}" alt="Manga page ${pageIndex+1}" referrerpolicy="no-referrer" onerror="this.insertAdjacentHTML('afterend','<p class=muted>Image could not be loaded by the browser.</p>')">`;document.getElementById("pageCount").textContent=`Page ${pageIndex+1} of ${currentImages.length}`; }
function changePage(n){if(!currentImages.length)return;pageIndex=Math.max(0,Math.min(currentImages.length-1,pageIndex+n));showPage()}
async function downloadChapter(){if(!document.getElementById("rights").checked){alert("Confirm you have permission to download this content.");return}if(!currentChapter)return;try{const d=await api("/api/manga/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:currentChapter.url,authorized:true})});location.href=d.download_url}catch(e){alert(e.message)}}
async function loadAnime(){try{const d=await api("/api/anime");animeItems=d.items||[];renderAnime()}catch(e){document.getElementById("animeGrid").textContent=e.message}}
function renderAnime(){const q=(document.getElementById("animeSearch").value||"").toLowerCase();const list=animeItems.filter(x=>(x.title||"").toLowerCase().includes(q));document.getElementById("animeCount").textContent=list.length+" titles";document.getElementById("animeGrid").innerHTML=list.map(x=>`<article class="item">${x.cover?`<img class="cover" src="${esc(x.cover)}" alt="">`:""}<h3>${esc(x.title)}</h3><span class="pill">${esc(x.type||"link")}</span><p class="small">${esc(x.notes||"")}</p><a href="${esc(x.url)}" target="_blank" rel="noopener">Open link ↗</a></article>`).join("")||"<p class='muted'>No anime links have been added yet.</p>"}
async function addAnime(){const s=document.getElementById("adminStatus");s.textContent="Saving…";try{const d=await api("/api/anime",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:document.getElementById("adminPassword").value,title:document.getElementById("animeTitle").value,url:document.getElementById("animeUrl").value,type:document.getElementById("animeType").value,cover:document.getElementById("animeCover").value,notes:document.getElementById("animeNotes").value})});s.textContent="Saved ("+d.storage+").";["animeTitle","animeUrl","animeCover","animeNotes"].forEach(id=>document.getElementById(id).value="");}catch(e){s.textContent=e.message}}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
loadAnime();
</script>
</body></html>"""

@app.get("/")
def home():
    return render_template_string(PAGE)

@app.get("/health")
def health():
    return jsonify({"ok": True, "app": "AnimeVerse"})

@app.post("/api/manga/series")
def manga_series():
    data = request.get_json(silent=True) or {}
    if not data.get("authorized"):
        return jsonify(error="Confirm that you have permission to view this content."), 403
    url = (data.get("url") or "").strip()
    try:
        return jsonify(parse_series(url))
    except Exception as e:
        return jsonify(error=str(e)), 400

@app.post("/api/manga/chapter")
def manga_chapter():
    data = request.get_json(silent=True) or {}
    if not data.get("authorized"):
        return jsonify(error="Confirm that you have permission to view this content."), 403
    url = (data.get("url") or "").strip()
    try:
        result = parse_reader_page(url)
        if not result["images"]:
            return jsonify(error="No reader images were found. The page may require JavaScript or a site-supported browser."), 404
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 400

@app.post("/api/manga/download")
def manga_download():
    data = request.get_json(silent=True) or {}
    if not data.get("authorized"):
        return jsonify(error="Confirm that you have permission to download this content."), 403
    url = (data.get("url") or "").strip()
    try:
        parsed = parse_reader_page(url)
        if not parsed["images"]:
            return jsonify(error="No page images were found."), 404
        # Build a short-lived in-memory ZIP. Only public image URLs are fetched.
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            count = 0
            for i, image_url in enumerate(parsed["images"], 1):
                if not safe_public_url(image_url):
                    continue
                try:
                    r = HTTP.get(image_url, timeout=HTTP_TIMEOUT, stream=True)
                    r.raise_for_status()
                    content = bytearray()
                    for chunk in r.iter_content(65536):
                        content.extend(chunk)
                        if len(content) > MAX_IMAGE_BYTES:
                            raise ValueError("An image exceeded the download size limit.")
                    ctype = r.headers.get("Content-Type", "")
                    if not ctype.startswith("image/"):
                        continue
                    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
                        ext = ".jpg"
                    z.writestr(f"page_{i:03d}{ext}", bytes(content))
                    count += 1
                except Exception:
                    continue
            if not count:
                return jsonify(error="No downloadable images were retrieved. The image host may block server requests."), 400
        mem.seek(0)
        import uuid
        token = uuid.uuid4().hex
        DOWNLOADS[token] = mem.getvalue()
        return jsonify(download_url=f"/download/{token}")
    except Exception as e:
        return jsonify(error=str(e)), 400

DOWNLOADS = {}

@app.get("/download/<token>")
def download_file(token):
    content = DOWNLOADS.pop(token, None)
    if content is None:
        abort(404)
    return Response(content, mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="manga_chapter.zip"'})

@app.get("/api/anime")
def anime_list():
    return jsonify(items=load_anime_items())

@app.post("/api/anime")
def anime_add():
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password") or "")
    if not ADMIN_PASSWORD or not __import__("hmac").compare_digest(supplied, ADMIN_PASSWORD):
        return jsonify(error="Incorrect admin password."), 403
    title = clean_text(data.get("title"))
    url = (data.get("url") or "").strip()
    if not title or not url or not safe_public_url(url):
        return jsonify(error="Enter a title and a valid public HTTP(S) link."), 400
    cover = (data.get("cover") or "").strip()
    if cover and not safe_public_url(cover):
        return jsonify(error="Cover image must be a public HTTP(S) URL."), 400
    item = {
        "id": __import__("uuid").uuid4().hex[:12],
        "title": title[:140],
        "url": url,
        "type": clean_text(data.get("type"))[:30] or "other",
        "cover": cover,
        "notes": clean_text(data.get("notes"))[:500]
    }
    try:
        with lock:
            items = load_anime_items()
            items.insert(0, item)
            save_anime_items(items)
        return jsonify(ok=True, storage="GitHub" if GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO else "local file")
    except Exception as e:
        return jsonify(error="Could not save links: " + str(e)), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
