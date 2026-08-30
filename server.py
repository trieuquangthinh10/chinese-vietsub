import os
import uuid
import threading
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory
from werkzeug.utils import secure_filename

from translator import process

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "outputs"
UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

jobs = {}
jobs_lock = threading.Lock()

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

def set_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)

@app.after_request
def no_cache(response):
    if request.path.startswith(("/app.js", "/style.css")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.get("/")
def home():
    return send_file(BASE / "index.html")

@app.get("/app.js")
def app_js():
    return send_from_directory(BASE, "app.js", mimetype="application/javascript")

@app.get("/style.css")
def style_css():
    return send_from_directory(BASE, "style.css", mimetype="text/css")

@app.post("/api/upload")
def upload():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(ok=False, error="Chưa chọn video."), 400

    filename = secure_filename(f.filename) or "video.mp4"
    if Path(filename).suffix.lower() not in ALLOWED_EXT:
        return jsonify(ok=False, error="Định dạng video không được hỗ trợ."), 400

    job_id = uuid.uuid4().hex
    video_path = UPLOADS / f"{job_id}_{filename}"

    try:
        # Streaming save: avoids reading the whole video into RAM.
        f.save(video_path, buffer_size=1024 * 1024)
    except Exception as e:
        try:
            video_path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify(ok=False, error=f"Không thể lưu video: {e}"), 500

    with jobs_lock:
        jobs[job_id] = {
            "input": str(video_path),
            "output": None,
            "region": None,
            "progress": 5,
            "status": "uploaded",
            "status_text": "Đã tải video lên. Sẵn sàng dịch."
        }

    return jsonify(ok=True, job_id=job_id)

@app.post("/api/translate/<job_id>")
def translate(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(ok=False, error="Không tìm thấy video."), 404
        if job["status"] in ("processing", "done"):
            return jsonify(ok=True)

    data = request.get_json(silent=True) or {}
    region = data.get("region")
    if not isinstance(region, dict):
        region = {"x": 0.05, "y": 0.68, "w": 0.90, "h": 0.27}

    try:
        region = {
            "x": float(region.get("x", 0.05)),
            "y": float(region.get("y", 0.68)),
            "w": float(region.get("w", 0.90)),
            "h": float(region.get("h", 0.27)),
        }
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Vùng phụ đề không hợp lệ."), 400

    region["x"] = max(0.0, min(1.0, region["x"]))
    region["y"] = max(0.0, min(1.0, region["y"]))
    region["w"] = max(0.01, min(1.0 - region["x"], region["w"]))
    region["h"] = max(0.01, min(1.0 - region["y"], region["h"]))

    set_job(job_id, region=region)

    thread = threading.Thread(target=run_translation, args=(job_id,), daemon=True)
    thread.start()
    return jsonify(ok=True)

def run_translation(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        inp = job["input"]
        region = job["region"]

    try:
        set_job(job_id, status="processing", progress=6, status_text="Đang chuẩn bị video…")
        output_path = OUTPUTS / f"{job_id}_VIETSUB.mp4"

        last_progress = [-1]
        def callback(progress, text):
            progress = max(0, min(100, int(progress)))
            # Avoid needless dict churn while preserving visible updates.
            if progress != last_progress[0] or progress in (0, 100):
                last_progress[0] = progress
                set_job(job_id, progress=progress, status_text=str(text))

        process(inp, str(output_path), callback, region)

        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError("Không tìm thấy video đầu ra hợp lệ.")

        set_job(
            job_id,
            output=str(output_path),
            progress=100,
            status="done",
            status_text="🎉 Hoàn tất!"
        )
    except Exception as e:
        set_job(job_id, progress=0, status="error", error=str(e), status_text="❌ Có lỗi khi xử lý video.")

@app.get("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(status="unknown", error="Không tìm thấy job."), 404
        return jsonify(job)

@app.get("/api/result/<job_id>")
def result(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Không tìm thấy job."), 404
        output = job.get("output")

    if job.get("status") != "done" or not output or not Path(output).exists():
        return jsonify(error="Video chưa sẵn sàng."), 404

    return send_file(output, as_attachment=True, download_name="video_VIETSUB.mp4", mimetype="video/mp4")

@app.errorhandler(413)
def file_too_large(error):
    return jsonify(ok=False, error="Video quá lớn. Giới hạn là 2GB."), 413

@app.errorhandler(500)
def internal_error(error):
    return jsonify(ok=False, error="Server gặp lỗi khi xử lý yêu cầu."), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
