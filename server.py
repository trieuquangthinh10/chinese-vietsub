import os, uuid, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename
from translator import process

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "outputs"
FRONTEND = BASE
UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = Flask(__name__)
jobs = {}

@app.get("/")
def home():
    return send_file(BASE / "index.html")

@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(FRONTEND, name)

@app.post("/api/upload")
def upload():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Chưa nhận được video"), 400

    jid = str(uuid.uuid4())
    safe = secure_filename(f.filename) or "video.mp4"
    path = UPLOADS / f"{jid}_{safe}"
    f.save(path)

    jobs[jid] = {
        "input": str(path),
        "progress": 5,
        "status": "uploaded",
        "status_text": "Đã nhận video."
    }
    return jsonify(job_id=jid)

@app.post("/api/translate/<jid>")
def translate(jid):
    if jid not in jobs:
        return jsonify(error="Không tìm thấy job"), 404

    if jobs[jid]["status"] == "processing":
        return jsonify(ok=True)

    data = request.get_json(silent=True) or {}
    region = data.get("region")
    if not isinstance(region, dict):
        return jsonify(error="Chưa chọn vùng phụ đề"), 400

    try:
        region = {
            "x": float(region["x"]),
            "y": float(region["y"]),
            "w": float(region["w"]),
            "h": float(region["h"]),
        }
    except Exception:
        return jsonify(error="Vùng phụ đề không hợp lệ"), 400

    if region["w"] <= 0 or region["h"] <= 0:
        return jsonify(error="Vùng phụ đề không hợp lệ"), 400

    jobs[jid]["region"] = region
    threading.Thread(target=run, args=(jid,), daemon=True).start()
    return jsonify(ok=True)

def run(jid):
    try:
        jobs[jid].update(status="processing", progress=10, status_text="Khởi động AI…")
        out = OUTPUTS / f"{jid}_VIETSUB.mp4"

        def cb(p, text):
            jobs[jid].update(progress=int(p), status_text=text)

        process(jobs[jid]["input"], str(out), cb, jobs[jid]["region"])

        jobs[jid].update(
            output=str(out),
            progress=100,
            status="done",
            status_text="Hoàn tất!"
        )
    except Exception as e:
        jobs[jid].update(
            status="error",
            error=str(e),
            status_text="Lỗi khi xử lý."
        )

@app.get("/api/status/<jid>")
def status(jid):
    return jsonify(jobs.get(jid, {"status": "unknown"}))

@app.get("/api/result/<jid>")
def result(jid):
    job = jobs.get(jid)
    if not job or "output" not in job:
        return jsonify(error="Video chưa sẵn sàng"), 404
    return send_file(
        job["output"],
        as_attachment=True,
        download_name="video_VIETSUB.mp4"
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
