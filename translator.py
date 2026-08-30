import os
import json
import base64
import subprocess
import tempfile
import re
from pathlib import Path

from openai import OpenAI

OCR_MODEL = os.environ.get("OCR_MODEL", "gpt-4o-mini")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "gpt-4o-mini")
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "alloy")

OCR_STEP = float(os.environ.get("OCR_STEP", "1.5"))
MAX_VIDEO_SECONDS = int(os.environ.get("MAX_VIDEO_SECONDS", "600"))

def process(inp, out, cb, region):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2)

    with tempfile.TemporaryDirectory() as temp:
        td = Path(temp)

        cb(7, "Đang phân tích video…")
        width, height, duration = probe_video(inp)

        x = max(0, min(int(width * region["x"]), width - 2))
        y = max(0, min(int(height * region["y"]), height - 2))
        w = max(10, min(int(width * region["w"]), width - x))
        h = max(10, min(int(height * region["h"]), height - y))

        cb(10, "Đang quét vùng phụ đề…")
        frames_dir = td / "frames"
        frames_dir.mkdir()

        max_duration = min(duration, MAX_VIDEO_SECONDS)
        step = max(0.8, OCR_STEP)
        frame_count = max(1, int(max_duration / step))

        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "0", "-i", inp,
            "-t", str(max_duration),
            "-vf", f"fps=1/{step},crop={w}:{h}:{x}:{y},scale='min(1280,iw)':-2",
            "-q:v", "4",
            str(frames_dir / "%05d.jpg")
        ], check=True)

        frames = sorted(frames_dir.glob("*.jpg"))
        if not frames:
            raise RuntimeError("Không thể đọc frame của video.")

        raw = []
        for i, frame in enumerate(frames):
            timestamp = i * step
            try:
                text = clean_text(ocr_frame(client, frame))
                if text:
                    raw.append({"start": timestamp, "text": text})
            except Exception:
                pass
            cb(12 + int(33 * (i + 1) / len(frames)),
               f"Đang nhận diện phụ đề… {i + 1}/{len(frames)}")

        segments = group_ocr(raw)
        if not segments:
            raise RuntimeError("Không tìm thấy phụ đề Trung trong vùng đã chọn.")

        cb(48, "Đang dịch sang tiếng Việt…")
        translated = translate_segments(client, segments, cb)
        if not translated:
            raise RuntimeError("Không có nội dung để dịch.")

        srt = td / "vietsub.srt"
        write_srt(srt, translated)

        cb(72, "Đang tạo giọng lồng tiếng Việt…")
        voice_dir = td / "voices"
        voice_dir.mkdir()
        voice_track = td / "voice.wav"

        create_voice_track(client, translated, voice_dir, voice_track, duration, cb)
        if not voice_track.exists():
            raise RuntimeError("Không tạo được giọng lồng tiếng.")

        cb(93, "Đang che phụ đề cũ và hoàn thiện video…")
        render_video(inp, out, srt, voice_track, x, y, w, h)
        cb(100, "🎉 Hoàn tất! Video đã được dịch và lồng tiếng.")

def probe_video(path):
    result = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json", path
    ], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not stream:
        raise RuntimeError("Không tìm thấy luồng video.")
    duration = float(stream.get("duration") or 0) or float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Không xác định được thời lượng video.")
    return int(stream["width"]), int(stream["height"]), duration

def ocr_frame(client, frame):
    image_data = base64.b64encode(frame.read_bytes()).decode("ascii")
    prompt = """Đọc CHỈ phụ đề tiếng Trung đang hiển thị trong ảnh.
Bỏ qua người, mặt, logo, watermark, giao diện, cảnh vật và chữ trang trí.
Nếu không có phụ đề tiếng Trung, trả về EMPTY.
Chỉ trả về nội dung chữ, không giải thích."""
    response = client.chat.completions.create(
        model=OCR_MODEL,
        temperature=0,
        max_tokens=120,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + image_data,
                "detail": "low"
            }}
        ]}]
    )
    return (response.choices[0].message.content or "").strip()

def clean_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text.lower() in {"empty", "none", "no subtitle", "no chinese subtitle"}:
        return ""
    if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text):
        return ""
    return text

def group_ocr(raw):
    if not raw:
        return []
    result = []
    for item in raw:
        if not result:
            result.append({"start": item["start"], "end": item["start"] + 1.5, "zh": item["text"]})
            continue
        last = result[-1]
        if item["text"] == last["zh"] and item["start"] - last["end"] <= 2.0:
            last["end"] = item["start"] + 1.5
        else:
            last["end"] = max(last["end"], item["start"])
            result.append({"start": item["start"], "end": item["start"] + 1.5, "zh": item["text"]})
    return result

def translate_segments(client, segments, cb):
    results = []
    batch_size = 30
    total = len(segments)

    for start in range(0, total, batch_size):
        chunk = segments[start:start + batch_size]
        context = segments[max(0, start - 5):min(total, start + batch_size + 5)]
        context_text = "\n".join(x["zh"] for x in context)
        target_text = "\n".join(f"{i + 1}. {x['zh']}" for i, x in enumerate(chunk))

        prompt = f"""Dịch phụ đề tiếng Trung sang tiếng Việt tự nhiên để LỒNG TIẾNG.
Câu phải ngắn gọn, dễ nói, đúng ngữ cảnh, giữ tên nhân vật nhất quán, không giải thích.

CONTEXT:
{context_text}

TARGET:
{target_text}

Chỉ trả JSON:
{{"translations":["..."]}}
Phải có đúng {len(chunk)} câu."""

        response = client.chat.completions.create(
            model=TEXT_MODEL,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Bạn là biên dịch viên Trung-Việt chuyên nghiệp cho lời thoại lồng tiếng."},
                {"role": "user", "content": prompt}
            ]
        )
        data = json.loads(response.choices[0].message.content)
        translations = data.get("translations")
        if not isinstance(translations, list) or len(translations) != len(chunk):
            raise RuntimeError("AI trả về số lượng câu dịch không hợp lệ.")

        for seg, vi in zip(chunk, translations):
            results.append({"start": seg["start"], "end": seg["end"], "vi": str(vi).strip()})

        completed = min(start + batch_size, total)
        cb(48 + int(24 * completed / total), f"Đang dịch… {completed}/{total}")

    return results

def create_voice_track(client, segments, voice_dir, output, duration, cb):
    audio_files = []
    total = len(segments)

    # TTS requests are independent, so run a small bounded pool in parallel.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def make_one(item):
        i, seg = item
        text = seg["vi"].strip()
        if not text:
            return None
        filename = voice_dir / f"voice_{i:05d}.mp3"
        response = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            response_format="mp3"
        )
        response.write_to_file(str(filename))
        return seg, filename

    completed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(make_one, item) for item in enumerate(segments)]
        for future in as_completed(futures):
            item = future.result()
            completed += 1
            if item:
                audio_files.append(item)
            cb(72 + int(19 * completed / max(1, total)),
               f"Đang tạo giọng Việt… {completed}/{total}")

    audio_files.sort(key=lambda item: item[0]["start"])
    if not audio_files:
        raise RuntimeError("Không tạo được giọng Việt.")

    inputs = []
    filters = []
    for index, (seg, audio) in enumerate(audio_files):
        inputs += ["-i", str(audio)]
        delay = max(0, int(seg["start"] * 1000))
        filters.append(f"[{index}:a]adelay={delay}:all=1[voice{index}]")

    labels = "".join(f"[voice{i}]" for i in range(len(audio_files)))
    filters.append(f"{labels}amix=inputs={len(audio_files)}:duration=longest:dropout_transition=0[mixed]")

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs, "-filter_complex", ";".join(filters),
        "-map", "[mixed]", "-t", str(duration),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output)
    ], check=True)

def render_video(inp, out, srt, voice_track, x, y, w, h):
    srt_path = ffmpeg_path(srt)
    audio_check = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", inp],
        capture_output=True, text=True
    )
    has_audio = bool(audio_check.stdout.strip())

    video_filter = (
        f"[0:v]split=2[original][blur];"
        f"[blur]crop={w}:{h}:{x}:{y},boxblur=18:3[blurred];"
        f"[original][blurred]overlay={x}:{y}[clean];"
        f"[clean]subtitles='{srt_path}':force_style='"
        f"FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=1,MarginV=25'[video]"
    )

    if has_audio:
        fc = video_filter + ";[0:a]volume=0.18[original_audio];[original_audio][1:a]amix=inputs=2:duration=first:dropout_transition=2[audio]"
        maps = ["-map", "[video]", "-map", "[audio]"]
    else:
        fc = video_filter
        maps = ["-map", "[video]", "-map", "1:a:0"]

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", inp, "-i", str(voice_track),
        "-filter_complex", fc, *maps,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out
    ], check=True)

def write_srt(path, segments):
    with open(path, "w", encoding="utf-8-sig") as f:
        for number, seg in enumerate(segments, 1):
            start = max(0, seg["start"])
            end = max(start + 0.3, seg["end"])
            f.write(f"{number}\n{ts(start)} --> {ts(end)}\n{seg['vi']}\n\n")

def ffmpeg_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    return value.replace(":", r"\:").replace("'", r"\'")

def ts(seconds):
    ms = max(0, int(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"
