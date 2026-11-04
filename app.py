
import re
import time
import requests

from flask import Flask, render_template_string, jsonify, request
from urllib.parse import quote

app = Flask(__name__)

ANILIST_API = "https://graphql.anilist.co"
MANGATOWN = "https://www.mangatown.com/manga/"

cache = {"data": [], "updated": 0}
CACHE_SECONDS = 600


def make_slug(title):
    """Create a MangaTown-style manga slug."""
    title = title.lower().strip()
    title = title.replace("'", "")
    title = re.sub(r"[^a-z0-9]+", "_", title)
    return title.strip("_")


def get_manga(search=""):
    global cache

    if (
        not search
        and cache["data"]
        and time.time() - cache["updated"] < CACHE_SECONDS
    ):
        return cache["data"]

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
                    romaji
                    english
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
                format
                siteUrl
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
        headers={"User-Agent": "AnimeVerse/1.0"},
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if result.get("errors"):
        raise RuntimeError("AniList API returned an error.")

    results = result["data"]["Page"]["media"]

    for manga in results:
        titles = manga["title"]

        title = (
            titles.get("english")
            or titles.get("romaji")
            or titles.get("native")
            or "Unknown Manga"
        )

        manga["display_title"] = title
        manga["slug"] = make_slug(title)

        manga["cover"] = (
            manga.get("coverImage", {}).get("extraLarge")
            or manga.get("coverImage", {}).get("large")
            or ""
        )

        manga["description"] = manga.get("description") or (
            "No description available."
        )

        manga["mangatown_url"] = (
            MANGATOWN + quote(manga["slug"]) + "/"
        )

    if not search:
        cache["data"] = results
        cache["updated"] = time.time()

    return results


PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>AnimeVerse | MangaTown Library</title>

<style>
* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background: #090b13;
    color: #f2f3fa;
    font-family: Arial, sans-serif;
}

header {
    background: #111625;
    border-bottom: 1px solid #252c40;
    padding: 22px 5%;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 15px;
}

.logo {
    font-size: 26px;
    font-weight: 900;
    color: #a78bfa;
}

.logo span {
    color: white;
}

nav {
    color: #a3a9bc;
    font-size: 13px;
}

.hero {
    padding: 55px 5%;
    background: linear-gradient(120deg, #211839, #10192c);
}

.hero h1 {
    font-size: clamp(32px, 5vw, 48px);
    margin-bottom: 15px;
}

.hero h1 span {
    color: #a78bfa;
}

.hero p {
    color: #b1b5c6;
    max-width: 650px;
    line-height: 1.7;
}

.stats {
    display: flex;
    gap: 40px;
    margin-top: 28px;
    flex-wrap: wrap;
}

.stat strong {
    color: #a78bfa;
    font-size: 26px;
    display: block;
}

.stat small {
    color: #a2a7b9;
}

main {
    padding: 35px 5%;
}

.toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 30px;
}

input {
    background: #151b2b;
    border: 1px solid #30364b;
    border-radius: 9px;
    color: white;
    padding: 14px;
    outline: none;
}

input:focus {
    border-color: #a78bfa;
}

#search {
    flex: 1;
    min-width: 200px;
}

#chapter, #page {
    width: 115px;
}

button {
    background: #7958d8;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 13px 20px;
    font-weight: bold;
    cursor: pointer;
}

button:hover {
    background: #9577ed;
}

.heading {
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 23px;
}

.heading span {
    color: #949bb0;
    font-size: 13px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(185px, 1fr));
    gap: 22px;
}

.card {
    background: #141a29;
    border: 1px solid #282e41;
    border-radius: 12px;
    overflow: hidden;
    transition: .2s;
}

.card:hover {
    transform: translateY(-5px);
    border-color: #9878f0;
}

.cover {
    width: 100%;
    height: 270px;
    object-fit: cover;
    background: #252b3b;
}

.content {
    padding: 14px;
}

.rank {
    color: #a78bfa;
    font-size: 12px;
    font-weight: bold;
    margin-bottom: 8px;
}

.content h3 {
    font-size: 15px;
    line-height: 1.4;
    min-height: 42px;
    margin-bottom: 9px;
}

.metadata {
    color: #a0a6ba;
    font-size: 12px;
    line-height: 1.7;
}

.score {
    color: #f4c66b;
}

.genres {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 10px;
}

.genre {
    background: #302648;
    color: #d0bfff;
    padding: 5px 7px;
    border-radius: 5px;
    font-size: 10px;
}

.actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 15px;
}

.actions a {
    text-align: center;
    text-decoration: none;
    border-radius: 7px;
    padding: 10px;
    font-size: 12px;
    font-weight: bold;
}

.read {
    background: #7958d8;
    color: white;
}

.details {
    background: #283147;
    color: #d3d8e6;
}

.status {
    grid-column: 1/-1;
    text-align: center;
    padding: 45px;
    color: #a4a9ba;
}

footer {
    text-align: center;
    padding: 30px;
    border-top: 1px solid #272d40;
    color: #858ca0;
    font-size: 12px;
}

@media(max-width: 500px) {
    .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
    }

    .cover {
        height: 220px;
    }
}
</style>
</head>

<body>

<header>
    <div class="logo">ANIME<span>VERSE</span></div>
    <nav>MANGA LIBRARY &nbsp; | &nbsp; TOP 100 &nbsp; | &nbsp; DISCOVER</nav>
</header>

<section class="hero">
    <h1>Explore the <span>Manga Universe.</span></h1>

    <p>
        Discover 100 of the highest-rated manga series.
        Browse covers, descriptions, rankings and chapter links
        through the MangaTown URL format.
    </p>

    <div class="stats">
        <div class="stat">
            <strong id="total">100</strong>
            <small>Manga titles</small>
        </div>
        <div class="stat">
            <strong>100</strong>
            <small>Ranked collection</small>
        </div>
        <div class="stat">
            <strong>LIVE</strong>
            <small>Ranking source</small>
        </div>
    </div>
</section>

<main>

    <div class="toolbar">

        <input
            id="search"
            placeholder="Search anime or manga..."
        >

        <input
            id="chapter"
            type="number"
            min="1"
            value="1"
            placeholder="Chapter"
            title="Chapter number"
        >

        <input
            id="page"
            type="number"
            min="1"
            value="1"
            placeholder="Page"
            title="Page number"
        >

        <button onclick="loadManga()">Search</button>
        <button onclick="resetLibrary()">Top 100</button>

    </div>

    <div class="heading">
        <h2 id="heading">Top 100 Manga</h2>
        <span id="subtitle">Ranked by AniList score</span>
    </div>

    <div class="grid" id="grid">
        <div class="status">Loading manga...</div>
    </div>

</main>

<footer>
    ANIMEVERSE · Manga discovery library · Powered by AniList
</footer>

<script>

let mangaData = [];

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[c]);
}

function getChapter() {
    let value = document.getElementById("chapter").value;
    value = value.trim();

    if (!/^\d+(\.\d+)?$/.test(value) || Number(value) < 1) {
        value = "1";
    }

    return value;
}

function getPage() {
    let value = parseInt(document.getElementById("page").value);

    if (!Number.isFinite(value) || value < 1) {
        value = 1;
    }

    return value;
}

function createCard(manga, index) {

    const title = escapeHTML(manga.display_title);
    const description = escapeHTML(manga.description);
    const cover = escapeHTML(manga.cover);
    const slug = encodeURIComponent(manga.slug);

    const chapter = encodeURIComponent(getChapter());
    const page = getPage();

    const chapterURL =
        `https://www.mangatown.com/manga/${slug}/c${chapter}/${page}.html`;

    const score = manga.averageScore
        ? (manga.averageScore / 10).toFixed(1) + "/10"
        : "N/A";

    const chapters = manga.chapters ?? "Unknown";
    const popularity = manga.popularity ?? "Unknown";

    const genres = (manga.genres || [])
        .slice(0, 3)
        .map(g => `<span class="genre">${escapeHTML(g)}</span>`)
        .join("");

    return `
        <article class="card">

            <img
                class="cover"
                src="${cover}"
                alt="${title} cover"
                loading="lazy"
                onerror="this.style.visibility='hidden'"
            >

            <div class="content">

                <div class="rank">#${index + 1} RANKED</div>

                <h3>${title}</h3>

                <div class="metadata">
                    <span class="score">★ ${score}</span><br>
                    Chapters: ${chapters}<br>
                    Popularity: ${popularity}<br>
                    Status: ${escapeHTML(manga.status || "Unknown")}
                </div>

                <div class="genres">
                    ${genres}
                </div>

                <div class="actions">

                    <a
                        class="read"
                        href="${chapterURL}"
                        target="_blank"
                        rel="noopener noreferrer"
                    >
                        Open MangaTown Chapter
                    </a>

                    <a
                        class="details"
                        href="${escapeHTML(manga.siteUrl)}"
                        target="_blank"
                        rel="noopener noreferrer"
                    >
                        Manga Information
                    </a>

                    <button
                        onclick="showDescription(${index})"
                    >
                        Description
                    </button>

                </div>

                <div
                    id="description-${index}"
                    style="display:none; margin-top:12px;
                    color:#aeb4c6;font-size:12px;line-height:1.6;"
                >
                    ${description}
                </div>

            </div>
        </article>
    `;
}

function showDescription(index) {
    const element = document.getElementById(
        `description-${index}`
    );

    element.style.display =
        element.style.display === "none" ? "block" : "none";
}

function renderManga(items) {

    const grid = document.getElementById("grid");

    mangaData = items;

    document.getElementById("total").textContent = items.length;

    if (!items.length) {
        grid.innerHTML =
            '<div class="status">No manga found.</div>';
        return;
    }

    grid.innerHTML = items.map((manga, index) =>
        createCard(manga, index)
    ).join("");
}

async function loadManga() {

    const grid = document.getElementById("grid");

    const search = document.getElementById("search").value.trim();

    grid.innerHTML =
        '<div class="status">Loading manga library...</div>';

    try {

        const response = await fetch(
            "/api/manga?q=" + encodeURIComponent(search)
        );

        if (!response.ok) {
            throw new Error("Server returned " + response.status);
        }

        const data = await response.json();

        if (data.error) {
            throw new Error(data.error);
        }

        renderManga(data.manga);

        document.getElementById("heading").textContent =
            search ? "Search Results" : "Top 100 Manga";

        document.getElementById("subtitle").textContent =
            search ? "Matching titles" : "Ranked by AniList score";

    } catch (error) {

        grid.innerHTML =
            `<div class="status">
                Error loading manga: ${escapeHTML(error.message)}
                <br><br>
                Please try again.
            </div>`;
    }
}

function resetLibrary() {

    document.getElementById("search").value = "";
    document.getElementById("chapter").value = "1";
    document.getElementById("page").value = "1";

    loadManga();
}

document.getElementById("search").addEventListener(
    "keydown",
    event => {
        if (event.key === "Enter") loadManga();
    }
);

loadManga();

</script>

</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(PAGE)


@app.route("/api/manga")
def manga_api():
    search = request.args.get("q", "").strip()

    try:
        manga = get_manga(search)

        return jsonify({
            "count": len(manga),
            "manga": manga
        })

    except requests.RequestException:
        return jsonify({
            "error": "Could not connect to AniList. Try again later."
        }), 502

    except Exception as error:
        app.logger.exception("Manga API error")
        return jsonify({
            "error": str(error)
        }), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 8000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
