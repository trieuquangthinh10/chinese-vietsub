import os, json, base64, subprocess, tempfile, re
from pathlib import Path
from openai import OpenAI

def process(inp, out, cb, region):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        cb(15, "Đang đọc kích thước video…")
        width, height, duration = probe_video(inp)

        x = max(0, min(width - 1, int(width * region["x"])))
        y = max(0, min(height - 1, int(height * region["y"])))
        w = max(2, min(width - x, int(width * region["w"])))
        h = max(2, min(height - y, int(height * region["h"])))

        if w < 10 or h < 10:
            raise RuntimeError("Vùng phụ đề quá nhỏ.")

        # OCR mỗi 1 giây. Với video dài, giới hạn 10 phút để tránh tạo quá nhiều request.
        sample_step = 1.0
        max_duration = min(duration, 600.0)
        frame_dir = td / "frames"
        frame_dir.mkdir()

        cb(20, "Đang quét chữ Trung trong vùng đã chọn…")

        total = max(1, int(max_duration / sample_step))
        subprocess.run([
            "ffmpeg", "-y", "-i", inp,
            "-vf", f"fps=1/{sample_step},crop={w}:{h}:{x}:{y},scale='min(1280,iw)':-2",
            "-frames:v", str(total),
            str(frame_dir / "%05d.jpg")
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        raw = []
        frames = sorted(frame_dir.glob("*.jpg"))

        for idx, frame in enumerate(frames):
            t = idx * sample_step
            text = ocr_frame(client, frame)

            if text:
                text = clean_text(text)
                if text:
                    raw.append({"start": t, "text": text})

            cb(20 + int(35 * (idx + 1) / max(1, len(frames))),
               f"OCR phụ đề… {idx + 1}/{len(frames)}")

        segments = group_ocr(raw)

        if not segments:
            raise RuntimeError("Không nhận diện được chữ Trung trong vùng đã chọn.")

        cb(58, "Đang dịch phụ đề theo ngữ cảnh…")
        translated = translate_segments(client, segments, cb)

        srt = td / "vietsub.srt"
        write_srt(srt, translated)

        cb(90, "Đang che phụ đề cũ và render tiếng Việt…")

        # Làm mờ đúng vùng cũ, sau đó phủ SRT mới.
        # boxblur trên crop + overlay giữ nguyên toàn bộ video.
        srt_path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
        filter_complex = (
            f"[0:v]split=2[base][blur];"
            f"[blur]crop={w}:{h}:{x}:{y},boxblur=12:2[blurred];"
            f"[base][blurred]overlay={x}:{y}[clean];"
            f"[clean]subtitles='{srt_path}':"
            f"force_style='FontName=Arial,FontSize=22,"
            f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
            f"BorderStyle=1,Outline=2,Shadow=1,MarginV=0'"
        )

        subprocess.run([
            "ffmpeg", "-y",
            "-i", inp,
            "-filter_complex", filter_complex,
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-c:a", "copy",
            "-movflags", "+faststart",
            out
        ], check=True)

def probe_video(path):
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json", path
    ], check=True, capture_output=True, text=True)

    data = json.loads(r.stdout)["streams"][0]
    return int(data["width"]), int(data["height"]), float(data.get("duration") or 0)

def ocr_frame(client, frame):
    data = base64.b64encode(frame.read_bytes()).decode("utf-8")
    prompt = (
        "Read ONLY the visible Chinese subtitle text in this image. "
        "Ignore people, logos, UI, watermarks and scenery. "
        "Return plain text only. If there is no Chinese subtitle, return EMPTY."
    )

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{data}",
                        "detail": "low"
                    }
                }
            ]
        }]
    )
    return (r.choices[0].message.content or "").strip()

def clean_text(s):
    s = re.sub(r"\s+", " ", s).strip()
    if s.lower() in {"empty", "none", "no subtitle", "không có"}:
        return ""
    return s

def group_ocr(raw):
    if not raw:
        return []

    result = []
    for item in raw:
        if not result:
            result.append({"start": item["start"], "end": item["start"] + 1.2, "zh": item["text"]})
            continue

        last = result[-1]
        if item["text"] == last["zh"] and item["start"] - last["end"] <= 1.5:
            last["end"] = item["start"] + 1.2
        else:
            last["end"] = max(last["end"], item["start"])
            result.append({
                "start": item["start"],
                "end": item["start"] + 1.2,
                "zh": item["text"]
            })

    return result

def translate_segments(client, segments, cb):
    vals = []
    for start in range(0, len(segments), 15):
        chunk = segments[start:start + 15]
        context = segments[max(0, start - 5):min(len(segments), start + 20)]

        prompt = (
            "Dịch phụ đề tiếng Trung sang tiếng Việt tự nhiên, đúng ngữ cảnh. "
            "Giữ tên riêng nhất quán, câu ngắn gọn phù hợp phụ đề. "
            "Chỉ trả JSON dạng {\"translations\":[...]}, đúng số lượng câu.\n\n"
            "CONTEXT:\n" +
            "\n".join(x["zh"] for x in context) +
            "\n\nTARGET:\n" +
            "\n".join(f"{i+1}. {x['zh']}" for i, x in enumerate(chunk))
        )

        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Bạn là biên dịch viên phụ đề Trung-Việt chuyên nghiệp."},
                {"role": "user", "content": prompt}
            ]
        )

        data = json.loads(r.choices[0].message.content)
        tr = data.get("translations")

        if not isinstance(tr, list) or len(tr) != len(chunk):
            raise RuntimeError("AI trả về số lượng phụ đề không hợp lệ.")

        for seg, vi in zip(chunk, tr):
            vals.append({
                "start": seg["start"],
                "end": seg["end"],
                "vi": str(vi).strip()
            })

        cb(58 + int(25 * min(start + 15, len(segments)) / len(segments)),
           f"Đang dịch… {min(start + 15, len(segments))}/{len(segments)}")

    return vals

def write_srt(path, segments):
    with open(path, "w", encoding="utf-8-sig") as f:
        for i, s in enumerate(segments, 1):
            f.write(
                f"{i}\n"
                f"{ts(s['start'])} --> {ts(s['end'])}\n"
                f"{s['vi']}\n\n"
            )

def ts(x):
    ms = max(0, int(x * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"
