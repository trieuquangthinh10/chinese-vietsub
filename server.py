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

# Cho phép upload video lớn
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

jobs = {}


# =========================================================
# WEB
# =========================================================

@app.get("/")
def home():
    return send_file(BASE / "index.html")


@app.get("/app.js")
def app_js():
    return send_from_directory(
        BASE,
        "app.js",
        mimetype="application/javascript"
    )


@app.get("/style.css")
def style_css():
    return send_from_directory(
        BASE,
        "style.css",
        mimetype="text/css"
    )


# =========================================================
# UPLOAD
# =========================================================

@app.post("/api/upload")
def upload():

    f = request.files.get("video")

    if not f or not f.filename:
        return jsonify(
            ok=False,
            error="Chưa chọn video."
        ), 400

    filename = secure_filename(f.filename)

    if not filename:
        filename = "video.mp4"

    job_id = str(uuid.uuid4())

    video_path = UPLOADS / f"{job_id}_{filename}"

    try:
        f.save(video_path)
    except Exception as e:
        return jsonify(
            ok=False,
            error=f"Không thể lưu video: {e}"
        ), 500

    jobs[job_id] = {
        "input": str(video_path),
        "output": None,
        "region": None,
        "progress": 5,
        "status": "uploaded",
        "status_text": "Đã nhận video."
    }

    return jsonify(
        ok=True,
        job_id=job_id
    )


# =========================================================
# START TRANSLATION
# =========================================================

@app.post("/api/translate/<job_id>")
def translate(job_id):

    if job_id not in jobs:
        return jsonify(
            ok=False,
            error="Không tìm thấy video."
        ), 404

    job = jobs[job_id]

    if job["status"] == "processing":
        return jsonify(ok=True)

    data = request.get_json(
        silent=True
    ) or {}

    region = data.get("region")

    # Vùng mặc định:
    # 5% từ trái
    # 68% từ trên
    # rộng 90%
    # cao 27%
    if not isinstance(region, dict):
        region = {
            "x": 0.05,
            "y": 0.68,
            "w": 0.90,
            "h": 0.27
        }

    try:
        region = {
            "x": float(region.get("x", 0.05)),
            "y": float(region.get("y", 0.68)),
            "w": float(region.get("w", 0.90)),
            "h": float(region.get("h", 0.27))
        }
    except (TypeError, ValueError):
        return jsonify(
            ok=False,
            error="Vùng phụ đề không hợp lệ."
        ), 400

    # Giới hạn
    region["x"] = max(
        0.0,
        min(1.0, region["x"])
    )

    region["y"] = max(
        0.0,
        min(1.0, region["y"])
    )

    region["w"] = max(
        0.01,
        min(1.0, region["w"])
    )

    region["h"] = max(
        0.01,
        min(1.0, region["h"])
    )

    # Không cho vùng vượt màn hình
    if region["x"] + region["w"] > 1:
        region["w"] = 1 - region["x"]

    if region["y"] + region["h"] > 1:
        region["h"] = 1 - region["y"]

    job["region"] = region

    threading.Thread(
        target=run_translation,
        args=(job_id,),
        daemon=True
    ).start()

    return jsonify(ok=True)


# =========================================================
# PROCESS
# =========================================================

def run_translation(job_id):

    job = jobs[job_id]

    try:

        job.update(
            status="processing",
            progress=10,
            status_text="Đang chuẩn bị video…"
        )

        output_path = OUTPUTS / (
            f"{job_id}_VIETSUB.mp4"
        )

        def callback(progress, text):

            job.update(
                progress=max(
                    0,
                    min(100, int(progress))
                ),
                status_text=str(text)
            )

        process(
            job["input"],
            str(output_path),
            callback,
            job["region"]
        )

        if not output_path.exists():
            raise RuntimeError(
                "Quá trình xử lý kết thúc nhưng không tìm thấy video đầu ra."
            )

        job.update(
            output=str(output_path),
            progress=100,
            status="done",
            status_text="🎉 Hoàn tất!"
        )

    except Exception as e:

        job.update(
            progress=0,
            status="error",
            error=str(e),
            status_text="❌ Có lỗi khi xử lý video."
        )


# =========================================================
# STATUS
# =========================================================

@app.get("/api/status/<job_id>")
def status(job_id):

    if job_id not in jobs:
        return jsonify(
            status="unknown",
            error="Không tìm thấy job."
        ), 404

    return jsonify(jobs[job_id])


# =========================================================
# RESULT
# =========================================================

@app.get("/api/result/<job_id>")
def result(job_id):

    if job_id not in jobs:
        return jsonify(
            error="Không tìm thấy job."
        ), 404

    job = jobs[job_id]

    output = job.get("output")

    if (
        job.get("status") != "done"
        or not output
        or not Path(output).exists()
    ):
        return jsonify(
            error="Video chưa sẵn sàng."
        ), 404

    return send_file(
        output,
        as_attachment=True,
        download_name="video_VIETSUB.mp4",
        mimetype="video/mp4"
    )


# =========================================================
# ERROR: FILE QUÁ LỚN
# =========================================================

@app.errorhandler(413)
def file_too_large(error):

    return jsonify(
        ok=False,
        error="Video quá lớn. Giới hạn là 2GB."
    ), 413


# =========================================================
# SERVER
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
