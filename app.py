
import os
import re
import json
import uuid
import base64
import hmac
import requests

from urllib.parse import quote, urlparse
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

ANILIST_API = "https://graphql.anilist.co"

# GitHub configuration: set these in Blitz Environment Variables.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE = "data/anime_links.json"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

manga_cache = None


# ============================================
# ANILIST API
# ============================================

def anilist_query(search=""):
    query = """
    query ($page: Int, $perPage: Int, $search: String) {
        Page(page: $page, perPage: $perPage) {
            media(
                type: MANGA,
                search: $search,
                sort: SCORE_DESC,
                isAdult: false
            ) {
                id
                title {
                    english
                    romaji
                    native
                }
                description(asHtml: false)
                coverImage {
                    extraLarge
                    large
                }
                averageScore
                popularity
                chapters
                volumes
                status
                genres
                siteUrl
                format
            }
        }
    }
    """

    response = requests.post(
        ANILIST_API,
        json={
            "query": query,
            "variables": {
                "page": 1,
                "perPage": 100,
                "search": search or None
            }
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if result.get("errors"):
        raise RuntimeError("AniList API error")

    mangas = result["data"]["Page"]["media"]

    for manga in mangas:
        titles = manga["title"]

        manga["display_title"] = (
            titles.get("english")
            or titles.get("romaji")
            or titles.get("native")
            or "Unknown"
        )

        manga["cover"] = (
            manga["coverImage"].get("extraLarge")
            or manga["coverImage"].get("large")
            or ""
        )

        manga["description"] = (
            manga.get("description") or "No description available."
        )

        slug = re.sub(
            r"[^a-z0-9]+",
            "_",
            manga["display_title"].lower()
        ).strip("_")

        manga["mangatown_url"] = (
            f"https://www.mangatown.com/manga/{quote(slug)}/"
        )

    return mangas


# ============================================
# GITHUB DATABASE
# ============================================

def github_configured():
    return all([
        GITHUB_TOKEN,
        GITHUB_OWNER,
        GITHUB_REPO
    ])


def github_url():
    return (
        f"https://api.github.com/repos/"
        f"{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    )


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def read_catalog():
    if not github_configured():
        raise RuntimeError("GitHub is not configured.")

    response = requests.get(
        github_url(),
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=20
    )

    if response.status_code == 404:
        return {"entries": []}, None

    response.raise_for_status()

    file_data = response.json()

    content = base64.b64decode(
        file_data["content"]
    ).decode("utf-8")

    return json.loads(content), file_data["sha"]


def write_catalog(catalog, sha=None):
    content = json.dumps(
        catalog,
        indent=4,
        ensure_ascii=False
    )

    encoded = base64.b64encode(
        content.encode("utf-8")
    ).decode("utf-8")

    payload = {
        "message": "Update AnimeVerse media library",
        "content": encoded,
        "branch": GITHUB_BRANCH
    }

    if sha:
        payload["sha"] = sha

    response = requests.put(
        github_url(),
        headers=github_headers(),
        json=payload,
        timeout=30
    )

    response.raise_for_status()


# ============================================
# ADMIN AUTHENTICATION
# ============================================

def authorized(data):
    supplied = str(data.get("password", ""))
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(
        supplied,
        ADMIN_PASSWORD
    )


# ============================================
# MANGA LIBRARY API
# ============================================

@app.route("/api/manga")
def manga_api():
    global manga_cache

    search = request.args.get("q", "").strip()

    try:
        if not search and manga_cache is not None:
            mangas = manga_cache
        else:
            mangas = anilist_query(search)

            if not search:
                manga_cache = mangas

        return jsonify({
            "success": True,
            "count": len(mangas),
            "manga": mangas
        })

    except Exception:
        app.logger.exception("AniList request failed")
        return jsonify({
            "success": False,
            "error": "Could not load manga. Please try again."
        }), 502


# ============================================
# MEDIA LIBRARY API
# ============================================

@app.route("/api/media")
def media_api():
    media_id = request.args.get("media_id", "").strip()

    try:
        catalog, _ = read_catalog()

        entries = [
            entry for entry in catalog.get("entries", [])
            if str(entry.get("media_id")) == media_id
        ]

        return jsonify({
            "success": True,
            "entries": entries
        })

    except Exception:
        app.logger.exception("GitHub catalog read failed")
        return jsonify({
            "success": False,
            "error": "Unable to load GitHub media library."
        }), 502


# ============================================
# ADD MEDIA TO GITHUB
# ============================================

@app.route("/api/media", methods=["POST"])
def add_media():
    data = request.get_json(silent=True) or {}

    if not authorized(data):
        return jsonify({"error": "Invalid admin password."}), 401

    media_id = str(data.get("media_id", "")).strip()
    media_name = str(data.get("media_name", "")).strip()
    title = str(data.get("title", "")).strip()
    media_type = str(data.get("media_type", "")).strip()
    url = str(data.get("url", "")).strip()
    chapter = str(data.get("chapter", "")).strip()
    page = str(data.get("page", "")).strip()

    if not all([media_id, media_name, title, media_type, url]):
        return jsonify({"error": "Fill in all required fields."}), 400

    if len(media_name) > 150 or len(title) > 150:
        return jsonify({"error": "Title is too long."}), 400

    if len(url) > 2000:
        return jsonify({"error": "URL is too long."}), 400

    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return jsonify({"error": "Enter a valid HTTP or HTTPS URL."}), 400

    if parsed.username or parsed.password:
        return jsonify({"error": "URLs containing credentials are not allowed."}), 400

    allowed_types = [
        "video",
        "audio",
        "file",
        "manga_page"
    ]

    if media_type not in allowed_types:
        return jsonify({"error": "Invalid media type."}), 400

    if media_type == "manga_page":
        if not chapter.isdigit() or not page.isdigit():
            return jsonify({
                "error": "Manga pages require numeric chapter and page numbers."
            }), 400

    try:
        catalog, sha = read_catalog()

        entry = {
            "id": str(uuid.uuid4()),
            "media_id": media_id,
            "media_name": media_name,
            "title": title,
            "media_type": media_type,
            "url": url,
            "chapter": chapter,
            "page": page
        }

        catalog.setdefault("entries", []).append(entry)

        write_catalog(catalog, sha)

        return jsonify({
            "success": True,
            "entry": entry
        })

    except Exception:
        app.logger.exception("GitHub catalog write failed")
        return jsonify({
            "error": "Could not save to GitHub. Check repository permissions."
        }), 502


# ============================================
# DELETE MEDIA
# ============================================

@app.route("/api/media/delete", methods=["POST"])
def delete_media():
    data = request.get_json(silent=True) or {}

    if not authorized(data):
        return jsonify({"error": "Invalid admin password."}), 401

    entry_id = str(data.get("id", "")).strip()

    try:
        catalog, sha = read_catalog()

        original = catalog.get("entries", [])

        catalog["entries"] = [
            entry for entry in original
            if entry.get("id") != entry_id
        ]

        if len(original) == len(catalog["entries"]):
            return jsonify({"error": "Entry not found."}), 404

        write_catalog(catalog, sha)

        return jsonify({"success": True})

    except Exception:
        app.logger.exception("GitHub delete failed")
        return jsonify({"error": "Could not delete entry."}), 502


# ============================================
# FRONTEND
# ============================================

HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>AnimeVerse</title>

<style>
* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background: #090b13;
    color: #f1f2f8;
    font-family: Arial, sans-serif;
}

header {
    background: #111625;
    border-bottom: 1px solid #293047;
    padding: 20px 5%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 15px;
}

.logo {
    font-size: 25px;
    font-weight: 900;
    color: #a78bfa;
}

.logo span {
    color: white;
}

nav {
    display: flex;
    flex-wrap: wrap;
    gap: 9px;
}

nav button {
    background: transparent;
    color: #aeb4c6;
}

nav button.active,
nav button:hover {
    background: #29213f;
    color: #c5b1ff;
}

.hero {
    padding: 45px 5%;
    background: linear-gradient(120deg,#21183b,#111a30);
}

.hero h1 {
    font-size: clamp(30px,5vw,46px);
    margin-bottom: 12px;
}

.hero h1 span {
    color: #a78bfa;
}

.hero p {
    color: #b4b8c9;
    line-height: 1.7;
}

main {
    padding: 35px 5%;
}

.section {
    display: none;
}

.section.active {
    display: block;
}

.toolbar {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 25px;
}

input, select, textarea {
    background: #151b2b;
    color: white;
    border: 1px solid #30384e;
    border-radius: 8px;
    padding: 13px;
    width: 100%;
}

.toolbar input {
    flex: 1;
    min-width: 190px;
}

button {
    background: #7958d8;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 12px 17px;
    cursor: pointer;
    font-weight: bold;
}

button:hover {
    background: #9577ef;
}

h2 {
    margin-bottom: 20px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fill,minmax(180px,1fr));
    gap: 20px;
}

.card {
    background: #141a29;
    border: 1px solid #293047;
    border-radius: 11px;
    overflow: hidden;
}

.card img {
    width: 100%;
    height: 260px;
    object-fit: cover;
    background: #252b3b;
}

.card-content {
    padding: 13px;
}

.card h3 {
    font-size: 15px;
    margin-bottom: 9px;
}

.muted {
    color: #a2a9bc;
    font-size: 12px;
    line-height: 1.6;
}

.card button {
    width: 100%;
    margin-top: 10px;
}

.card a {
    display: block;
    text-align: center;
    text-decoration: none;
    background: #302548;
    color: #d5c8ff;
    padding: 11px;
    border-radius: 7px;
    margin-top: 8px;
    font-size: 12px;
}

.reader {
    background: #111725;
    border: 1px solid #30384e;
    border-radius: 12px;
    padding: 18px;
    text-align: center;
}

.reader img {
    display: block;
    max-width: 100%;
    height: auto;
    max-height: 85vh;
    margin: 15px auto;
    object-fit: contain;
}

.reader video,
.reader audio {
    width: 100%;
    margin: 15px 0;
}

.reader-controls {
    display: flex;
    justify-content: center;
    gap: 10px;
    flex-wrap: wrap;
    margin: 15px;
}

.form {
    max-width: 650px;
    background: #141a29;
    border: 1px solid #30384e;
    border-radius: 12px;
    padding: 22px;
}

.form label {
    display: block;
    margin: 15px 0 7px;
    color: #c5cadd;
    font-size: 13px;
}

.form button {
    margin-top: 18px;
}

.notice {
    margin: 15px 0;
    color: #b7a4ff;
    font-size: 13px;
}

.media-entry {
    background: #151b2b;
    padding: 15px;
    border-radius: 9px;
    margin-bottom: 12px;
}

.media-entry a {
    color: #bba8ff;
}

footer {
    text-align: center;
    padding: 30px;
    border-top: 1px solid #252d40;
    color: #858da2;
    font-size: 12px;
}

@media(max-width:500px) {
    .grid {
        grid-template-columns: repeat(2,minmax(0,1fr));
        gap: 11px;
    }

    .card img {
        height: 210px;
    }
}
</style>
</head>

<body>

<header>
    <div class="logo">ANIME<span>VERSE</span></div>

    <nav>
        <button class="active" onclick="showTab('library',this)">Manga</button>
        <button onclick="showTab('reader',this)">Reader</button>
        <button onclick="showTab('media',this)">Media Library</button>
        <button onclick="showTab('admin',this)">Admin</button>
    </nav>
</header>

<section class="hero">
    <h1>Your <span>Manga Universe.</span></h1>
    <p>
        Discover manga, read authorized image collections,
        and organize your anime media library.
    </p>
</section>

<main>

<!-- MANGA LIBRARY -->

<section id="library" class="section active">

    <h2>Top 100 Manga</h2>

    <div class="toolbar">
        <input id="search" placeholder="Search manga...">
        <button onclick="loadManga()">Search</button>
        <button onclick="resetSearch()">Top 100</button>
    </div>

    <div id="manga-grid" class="grid">
        Loading manga...
    </div>

</section>


<!-- MANGA READER -->

<section id="reader" class="section">

    <h2>Manga Reader</h2>

    <p class="muted">
        Read manga images added to your GitHub media catalog.
    </p>

    <div class="toolbar" style="margin-top:20px">
        <input id="reader-id" placeholder="AniList manga ID">
        <button onclick="loadReader()">Open manga</button>
    </div>

    <div id="reader-container" class="reader">
        Select a manga to begin.
    </div>

</section>


<!-- MEDIA LIBRARY -->

<section id="media" class="section">

    <h2>Anime Media Library</h2>

    <div class="toolbar">
        <input id="media-id" placeholder="Anime or manga ID">
        <button onclick="loadMedia()">Load media</button>
    </div>

    <div id="media-results"></div>

</section>


<!-- ADMIN -->

<section id="admin" class="section">

    <h2>Media Management</h2>

    <p class="muted">
        Add links to the GitHub catalog. Use the AniList ID
        to connect entries to the correct series.
    </p>

    <div class="form">

        <label>Admin password</label>
        <input type="password" id="password">

        <label>AniList ID</label>
        <input id="add-id" placeholder="Example: 113415">

        <label>Anime or manga title</label>
        <input id="add-name" placeholder="Jujutsu Kaisen">

        <label>Episode, chapter, or file name</label>
        <input id="add-title" placeholder="Chapter 001 - Page 1">

        <label>Media type</label>
        <select id="add-type">
            <option value="manga_page">Manga page image</option>
            <option value="video">Video</option>
            <option value="audio">Audio</option>
            <option value="file">Other file</option>
        </select>

        <label>Chapter number (manga images)</label>
        <input id="add-chapter" type="number" min="1" placeholder="1">

        <label>Page number (manga images)</label>
        <input id="add-page" type="number" min="1" placeholder="1">

        <label>Media URL</label>
        <input id="add-url" placeholder="https://example.com/file.mp4">

        <button onclick="addMedia()">Save to GitHub</button>

        <div id="admin-message" class="notice"></div>

    </div>

</section>

</main>

<footer>
    AnimeVerse · Flask · AniList · GitHub
</footer>

<script>

let mangaList = [];
let readerPages = [];
let currentPage = 0;

function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[c]);
}

function showTab(id, button) {
    document.querySelectorAll(".section").forEach(
        x => x.classList.remove("active")
    );

    document.getElementById(id).classList.add("active");

    document.querySelectorAll("nav button").forEach(
        x => x.classList.remove("active")
    );

    if (button) button.classList.add("active");
}

async function loadManga() {
    const grid = document.getElementById("manga-grid");
    const search = document.getElementById("search").value.trim();

    grid.innerHTML = "Loading manga...";

    try {
        const response = await fetch(
            "/api/manga?q=" + encodeURIComponent(search)
        );

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Could not load manga.");
        }

        mangaList = data.manga;

        if (!mangaList.length) {
            grid.innerHTML = "No manga found.";
            return;
        }

        grid.innerHTML = mangaList.map(m => `
            <article class="card">

                <img
                    src="${esc(m.cover)}"
                    alt="${esc(m.display_title)}"
                    loading="lazy"
                >

                <div class="card-content">

                    <h3>${esc(m.display_title)}</h3>

                    <div class="muted">
                        Score: ${m.averageScore ?? "N/A"} / 100<br>
                        Chapters: ${m.chapters ?? "Unknown"}<br>
                        Popularity: ${m.popularity ?? "Unknown"}
                    </div>

                    <button onclick="openReader('${m.id}')">
                        Open built-in reader
                    </button>

                    <button onclick="loadMediaFor('${m.id}')">
                        View media
                    </button>

                    <a href="${esc(m.mangatown_url)}"
                       target="_blank"
                       rel="noopener noreferrer">
                        Open MangaTown series
                    </a>

                    <a href="${esc(m.siteUrl)}"
                       target="_blank"
                       rel="noopener noreferrer">
                        Manga details
                    </a>

                </div>
            </article>
        `).join("");

    } catch(error) {
        grid.textContent = error.message;
    }
}

function resetSearch() {
    document.getElementById("search").value = "";
    loadManga();
}

function openReader(id) {
    document.getElementById("reader-id").value = id;
    showTab("reader");
    loadReader();
}

async function loadReader() {
    const id = document.getElementById("reader-id").value.trim();

    if (!id) return;

    const container = document.getElementById("reader-container");

    container.textContent = "Loading manga pages...";

    try {
        const response = await fetch(
            "/api/media?media_id=" + encodeURIComponent(id)
        );

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Could not load pages.");
        }

        readerPages = data.entries
            .filter(x => x.media_type === "manga_page")
            .sort((a,b) =>
                Number(a.chapter) - Number(b.chapter) ||
                Number(a.page) - Number(b.page)
            );

        currentPage = 0;

        if (!readerPages.length) {
            container.textContent =
                "No manga pages have been added to this series yet.";
            return;
        }

        renderReader();

    } catch(error) {
        container.textContent = error.message;
    }
}

function renderReader() {
    const container = document.getElementById("reader-container");

    if (!readerPages.length) return;

    const page = readerPages[currentPage];

    container.innerHTML = `
        <h3>${esc(page.media_name)}</h3>

        <p class="muted">
            Chapter ${esc(page.chapter)} ·
            Page ${esc(page.page)} ·
            ${currentPage + 1} of ${readerPages.length}
        </p>

        <img
            src="${esc(page.url)}"
            alt="${esc(page.title)}"
            referrerpolicy="no-referrer"
            onerror="this.alt='Image could not be loaded. Check the image URL and hosting permissions.'"
        >

        <div class="reader-controls">
            <button onclick="previousPage()"
                ${currentPage === 0 ? "disabled" : ""}>
                Previous
            </button>

            <button onclick="nextPage()"
                ${currentPage === readerPages.length - 1 ? "disabled" : ""}>
                Next
            </button>

            <a href="${esc(page.url)}"
               target="_blank"
               rel="noopener noreferrer">
                Open image
            </a>
        </div>
    `;
}

function previousPage() {
    if (currentPage > 0) {
        currentPage--;
        renderReader();
    }
}

function nextPage() {
    if (currentPage < readerPages.length - 1) {
        currentPage++;
        renderReader();
    }
}

async function loadMediaFor(id) {
    document.getElementById("media-id").value = id;
    showTab("media");
    await loadMedia();
}

async function loadMedia() {
    const id = document.getElementById("media-id").value.trim();

    const container = document.getElementById("media-results");

    if (!id) {
        container.textContent = "Enter an AniList ID.";
        return;
    }

    container.textContent = "Loading media...";

    try {
        const response = await fetch(
            "/api/media?media_id=" + encodeURIComponent(id)
        );

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Unable to load media.");
        }

        if (!data.entries.length) {
            container.textContent = "No media added yet.";
            return;
        }

        container.innerHTML = data.entries.map(entry => {

            let player = "";

            if (entry.media_type === "video") {
                player = `
                    <video controls preload="metadata">
                        <source src="${esc(entry.url)}">
                        Your browser does not support this video.
                    </video>
                `;
            } else if (entry.media_type === "audio") {
                player = `
                    <audio controls preload="metadata">
                        <source src="${esc(entry.url)}">
                    </audio>
                `;
            } else if (entry.media_type === "manga_page") {
                player = `
                    <img
                        src="${esc(entry.url)}"
                        style="max-width:100%;max-height:500px;object-fit:contain"
                        alt="${esc(entry.title)}"
                    >
                `;
            }

            return `
                <div class="media-entry">

                    <h3>${esc(entry.title)}</h3>

                    <p class="muted">
                        ${esc(entry.media_type)}
                        ${entry.chapter ? " · Chapter " + esc(entry.chapter) : ""}
                        ${entry.page ? " · Page " + esc(entry.page) : ""}
                    </p>

                    ${player}

                    <p style="margin-top:10px">
                        <a href="${esc(entry.url)}"
                           target="_blank"
                           rel="noopener noreferrer">
                            Open / Download file
                        </a>
                    </p>

                </div>
            `;
        }).join("");

    } catch(error) {
        container.textContent = error.message;
    }
}

async function addMedia() {
    const message = document.getElementById("admin-message");

    message.textContent = "Saving to GitHub...";

    const payload = {
        password: document.getElementById("password").value,
        media_id: document.getElementById("add-id").value,
        media_name: document.getElementById("add-name").value,
        title: document.getElementById("add-title").value,
        media_type: document.getElementById("add-type").value,
        chapter: document.getElementById("add-chapter").value,
        page: document.getElementById("add-page").value,
        url: document.getElementById("add-url").value
    };

    try {
        const response = await fetch("/api/media", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(data.error || "Save failed.");
        }

        message.textContent = "Saved successfully to GitHub!";

        document.getElementById("add-title").value = "";
        document.getElementById("add-url").value = "";
        document.getElementById("add-page").value = "";

    } catch(error) {
        message.textContent = error.message;
    }
}

loadManga();

</script>

</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
