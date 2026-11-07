
import os
from pathlib import Path
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this-secret")

BASE_DIR = Path(__file__).resolve().parent
ID_FILE = BASE_DIR / "manga_ids.txt"


def load_catalog():
    manga = []

    if not ID_FILE.exists():
        return manga

    lines = ID_FILE.read_text(encoding="utf-8").splitlines()

    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split("|")]

        if len(parts) != 3:
            print(f"Invalid entry on line {line_number}")
            continue

        name, raw_id, title_url = parts

        try:
            chapter1_id = int(raw_id)
        except ValueError:
            continue

        if not name:
            continue

        if not title_url.startswith(
            "https://mangaplus.shueisha.co.jp/"
        ):
            continue

        manga.append({
            "name": name,
            "chapter1": chapter1_id if chapter1_id > 0 else None,
            "title_url": title_url
        })

    return manga


@app.route("/")
def home():
    return render_template(
        "index.html",
        manga=load_catalog()
    )


@app.route("/api/chapter")
def get_chapter():
    name = request.args.get("manga", "").strip()

    try:
        chapter = int(request.args.get("chapter", "1"))
    except ValueError:
        return jsonify(error="Invalid chapter number"), 400

    if chapter < 1 or chapter > 9999:
        return jsonify(error="Chapter must be between 1 and 9999"), 400

    entry = next(
        (item for item in load_catalog()
         if item["name"] == name),
        None
    )

    if not entry:
        return jsonify(error="Manga not found"), 404

    if entry["chapter1"] is None:
        return jsonify(
            url=entry["title_url"],
            title_url=entry["title_url"],
            message="Select your chapter on the official series page."
        )

    # Estimate viewer ID from the supplied chapter-one ID.
    # MANGA Plus does not guarantee sequential chapter IDs.
    viewer_id = entry["chapter1"] + chapter - 1

    return jsonify(
        url=f"https://mangaplus.shueisha.co.jp/viewer/{viewer_id}",
        title_url=entry["title_url"],
        message="If this chapter link fails, select the chapter on the official series page."
    )


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
