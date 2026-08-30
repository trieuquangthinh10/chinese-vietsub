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

# Quét dày hơn để không bỏ sót subtitle xuất hiện ngắn.
OCR_STEP = float(os.environ.get("OCR_STEP", "0.8"))
MAX_VIDEO_SECONDS = int(os.environ.get("MAX_VIDEO_SECONDS", "600"))


def process(inp, out, cb, region):
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        max_retries=2
    )

    with tempfile.TemporaryDirectory() as temp:
        td = Path(temp)

        cb(7, "Đang phân tích video…")
        width, height, duration = probe_video(inp)

        # region từ frontend là tỷ lệ 0 -> 1.
        x = max(0, min(int(width * float(region.get("x", 0))), width - 2))
        y = max(0, min(int(height * float(region.get("y", 0))), height - 2))
        w = max(
            10,
            min(
                int(width * float(region.get("w", 1))),
                width - x
            )
        )
        h = max(
            10,
            min(
                int(height * float(region.get("h", 0.25))),
                height - y
            )
        )

        # Tránh vùng quá nhỏ khiến OCR rất khó đọc.
        if w < 80 or h < 20:
            raise RuntimeError(
                "Vùng phụ đề quá nhỏ. Hãy kéo vùng chọn lớn hơn."
            )

        cb(10, "Đang quét vùng phụ đề…")

        frames_dir = td / "frames"
        processed_dir = td / "processed"
        frames_dir.mkdir()
        processed_dir.mkdir()

        max_duration = min(duration, MAX_VIDEO_SECONDS)
        step = max(0.5, OCR_STEP)

        # Lấy frame gốc.
        subprocess.run([
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", inp,
            "-t", str(max_duration),
            "-vf",
            (
                f"fps=1/{step},"
                f"crop={w}:{h}:{x}:{y},"
                f"scale=1600:-2:flags=lanczos"
            ),
            "-q:v", "2",
            str(frames_dir / "%05d.jpg")
        ], check=True)

        frames = sorted(frames_dir.glob("*.jpg"))

        if not frames:
            raise RuntimeError("Không thể đọc frame của video.")

        # Tạo bản xử lý OCR:
        # grayscale + tăng tương phản + sharpen nhẹ.
        for frame in frames:
            processed = processed_dir / frame.name

            subprocess.run([
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(frame),
                "-vf",
                (
                    "format=gray,"
                    "eq=contrast=1.35:brightness=0.03,"
                    "unsharp=5:5:0.8:5:5:0"
                ),
                "-q:v", "2",
                str(processed)
            ], check=True)

        raw = []

        total_frames = len(frames)

        for i, frame in enumerate(frames):
            timestamp = i * step

            processed = processed_dir / frame.name

            try:
                # OCR ảnh gốc trước.
                text = clean_text(
                    ocr_frame(client, frame)
                )

                # Nếu không nhận được thì thử ảnh đã xử lý.
                if not text:
                    text = clean_text(
                        ocr_frame(client, processed)
                    )

                if text:
                    raw.append({
                        "start": timestamp,
                        "text": text
                    })

            except Exception:
                # Một frame lỗi không được làm chết toàn bộ video.
                pass

            cb(
                12 + int(33 * (i + 1) / total_frames),
                f"Đang nhận diện phụ đề… {i + 1}/{total_frames}"
            )

        segments = group_ocr(raw)

        if not segments:
            raise RuntimeError(
                "Không tìm thấy phụ đề Trung trong vùng đã chọn. "
                "Hãy kéo vùng chọn bao phủ toàn bộ dòng phụ đề."
            )

        cb(48, "Đang dịch sang tiếng Việt…")

        translated = translate_segments(
            client,
            segments,
            cb
        )

        if not translated:
            raise RuntimeError("Không có nội dung để dịch.")

        srt = td / "vietsub.srt"
        write_srt(srt, translated)

        cb(72, "Đang tạo giọng lồng tiếng Việt…")

        voice_dir = td / "voices"
        voice_dir.mkdir()

        voice_track = td / "voice.wav"

        create_voice_track(
            client,
            translated,
            voice_dir,
            voice_track,
            duration,
            cb
        )

        if not voice_track.exists():
            raise RuntimeError(
                "Không tạo được giọng lồng tiếng."
            )

        cb(93, "Đang che phụ đề cũ và hoàn thiện video…")

        render_video(
            inp,
            out,
            srt,
            voice_track,
            x,
            y,
            w,
            h
        )

        cb(
            100,
            "🎉 Hoàn tất! Video đã được dịch và lồng tiếng."
        )


def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "stream=codec_type,width,height,duration:"
            "format=duration",
            "-of", "json",
            path
        ],
        check=True,
        capture_output=True,
        text=True
    )

    data = json.loads(result.stdout)

    stream = next(
        (
            s
            for s in data.get("streams", [])
            if s.get("codec_type") == "video"
        ),
        None
    )

    if not stream:
        raise RuntimeError(
            "Không tìm thấy luồng video."
        )

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    duration = float(
        stream.get("duration") or 0
    )

    if duration <= 0:
        duration = float(
            data.get("format", {}).get("duration") or 0
        )

    if width <= 0 or height <= 0:
        raise RuntimeError(
            "Không đọc được kích thước video."
        )

    if duration <= 0:
        raise RuntimeError(
            "Không xác định được thời lượng video."
        )

    return width, height, duration


def ocr_frame(client, frame):
    image_data = base64.b64encode(
        frame.read_bytes()
    ).decode("ascii")

    prompt = """
Bạn đang làm OCR phụ đề video.

Hãy đọc CHỈ phần phụ đề tiếng Trung xuất hiện trong ảnh.

QUY TẮC:
- Chỉ đọc lời thoại/phụ đề tiếng Trung.
- Bỏ qua người, khuôn mặt, cảnh vật.
- Bỏ qua logo và watermark.
- Bỏ qua chữ trên quần áo, biển hiệu và vật thể.
- Không dịch.
- Không giải thích.
- Giữ nguyên chữ Trung.
- Nếu có nhiều dòng phụ đề, nối chúng bằng một dấu cách.
- Nếu không có phụ đề tiếng Trung rõ ràng, trả về EMPTY.

Chỉ trả về chữ Trung hoặc EMPTY.
"""

    response = client.chat.completions.create(
        model=OCR_MODEL,
        temperature=0,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url":
                                "data:image/jpeg;base64,"
                                + image_data,
                            "detail": "high"
                        }
                    }
                ]
            }
        ]
    )

    return (
        response.choices[0].message.content or ""
    ).strip()


def clean_text(text):
    if not text:
        return ""

    text = text.strip()

    # Xóa markdown / dấu bao quanh nếu model vô tình thêm.
    text = re.sub(
        r"^```(?:text)?\s*|\s*```$",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    lower = text.lower()

    invalid = {
        "empty",
        "none",
        "null",
        "no subtitle",
        "no subtitles",
        "no chinese subtitle",
        "no chinese subtitles",
        "không có phụ đề"
    }

    if lower in invalid:
        return ""

    # Phải có ít nhất một ký tự CJK.
    if not re.search(
        r"[\u3400-\u4dbf\u4e00-\u9fff]",
        text
    ):
        return ""

    # Loại bỏ các câu trả lời giải thích không cần thiết.
    text = re.sub(
        r"^(phụ đề[:：]\s*)",
        "",
        text,
        flags=re.IGNORECASE
    ).strip()

    return text


def group_ocr(raw):
    if not raw:
        return []

    result = []

    for item in raw:
        text = item["text"]
        start = float(item["start"])

        if not text:
            continue

        if not result:
            result.append({
                "start": start,
                "end": start + max(1.2, OCR_STEP + 0.4),
                "zh": text
            })
            continue

        last = result[-1]

        # Cùng câu xuất hiện liên tục.
        if similar_text(text, last["zh"]):
            if start - last["end"] <= 2.5:
                last["end"] = max(
                    last["end"],
                    start + max(1.2, OCR_STEP + 0.4)
                )
                continue

        # Câu mới.
        last["end"] = max(
            last["end"],
            start
        )

        result.append({
            "start": start,
            "end": start + max(1.2, OCR_STEP + 0.4),
            "zh": text
        })

    # Không để segment cuối dài quá vô lý.
    for segment in result:
        if segment["end"] <= segment["start"]:
            segment["end"] = segment["start"] + 1.2

    return result


def similar_text(a, b):
    a = normalize_ocr_text(a)
    b = normalize_ocr_text(b)

    if a == b:
        return True

    # OCR đôi lúc thêm/bớt một dấu câu.
    a2 = re.sub(r"[，。！？、,.!?：:；;\"'「」『』\s]", "", a)
    b2 = re.sub(r"[，。！？、,.!?：:；;\"'「」『』\s]", "", b)

    return a2 == b2


def normalize_ocr_text(text):
    return re.sub(
        r"\s+",
        " ",
        text.strip()
    )


def translate_segments(client, segments, cb):
    results = []

    batch_size = 30
    total = len(segments)

    for start in range(0, total, batch_size):
        chunk = segments[
            start:start + batch_size
        ]

        context = segments[
            max(0, start - 5):
            min(total, start + batch_size + 5)
        ]

        context_text = "\n".join(
            x["zh"]
            for x in context
        )

        target_text = "\n".join(
            f"{i + 1}. {x['zh']}"
            for i, x in enumerate(chunk)
        )

        prompt = f"""
Dịch phụ đề tiếng Trung sang tiếng Việt tự nhiên để LỒNG TIẾNG.

Yêu cầu:
- Dịch đúng ngữ cảnh.
- Câu ngắn gọn, dễ nói.
- Không dịch máy cứng nhắc.
- Giữ tên nhân vật nhất quán.
- Không giải thích.
- Không thêm nội dung.
- Giữ đúng thứ tự câu.

CONTEXT:
{context_text}

TARGET:
{target_text}

Chỉ trả JSON:
{{"translations":["..."]}}

Phải có đúng {len(chunk)} câu.
"""

        response = client.chat.completions.create(
            model=TEXT_MODEL,
            temperature=0.2,
            response_format={
                "type": "json_object"
            },
            messages=[
                {
                    "role": "system",
                    "content":
                        "Bạn là biên dịch viên "
                        "Trung-Việt chuyên nghiệp "
                        "cho lời thoại lồng tiếng."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        data = json.loads(
            response.choices[0].message.content
        )

        translations = data.get(
            "translations"
        )

        if (
            not isinstance(translations, list)
            or len(translations) != len(chunk)
        ):
            raise RuntimeError(
                "AI trả về số lượng câu dịch không hợp lệ."
            )

        for seg, vi in zip(
            chunk,
            translations
        ):
            results.append({
                "start": seg["start"],
                "end": seg["end"],
                "vi": str(vi).strip()
            })

        completed = min(
            start + batch_size,
            total
        )

        cb(
            48 + int(
                24 * completed / total
            ),
            f"Đang dịch… {completed}/{total}"
        )

    return results


def create_voice_track(
    client,
    segments,
    voice_dir,
    output,
    duration,
    cb
):
    audio_files = []

    total = len(segments)

    from concurrent.futures import (
        ThreadPoolExecutor,
        as_completed
    )

    def make_one(item):
        i, seg = item

        text = seg["vi"].strip()

        if not text:
            return None

        filename = (
            voice_dir / f"voice_{i:05d}.mp3"
        )

        response = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            response_format="mp3"
        )

        response.write_to_file(
            str(filename)
        )

        return seg, filename

    completed = 0

    with ThreadPoolExecutor(
        max_workers=4
    ) as pool:

        futures = [
            pool.submit(
                make_one,
                item
            )
            for item in enumerate(segments)
        ]

        for future in as_completed(futures):
            item = future.result()

            completed += 1

            if item:
                audio_files.append(item)

            cb(
                72 + int(
                    19 * completed /
                    max(1, total)
                ),
                f"Đang tạo giọng Việt… "
                f"{completed}/{total}"
            )

    audio_files.sort(
        key=lambda item: item[0]["start"]
    )

    if not audio_files:
        raise RuntimeError(
            "Không tạo được giọng Việt."
        )

    inputs = []
    filters = []

    for index, (seg, audio) in enumerate(
        audio_files
    ):
        inputs += [
            "-i",
            str(audio)
        ]

        delay = max(
            0,
            int(seg["start"] * 1000)
        )

        filters.append(
            f"[{index}:a]"
            f"adelay={delay}:all=1"
            f"[voice{index}]"
        )

    labels = "".join(
        f"[voice{i}]"
        for i in range(
            len(audio_files)
        )
    )

    filters.append(
        f"{labels}"
        f"amix=inputs={len(audio_files)}:"
        f"duration=longest:"
        f"dropout_transition=0"
        f"[mixed]"
    )

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map", "[mixed]",
            "-t", str(duration),
            "-ar", "48000",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(output)
        ],
        check=True
    )


def render_video(
    inp,
    out,
    srt,
    voice_track,
    x,
    y,
    w,
    h
):
    srt_path = ffmpeg_path(srt)

    audio_check = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            inp
        ],
        capture_output=True,
        text=True
    )

    has_audio = bool(
        audio_check.stdout.strip()
    )

    video_filter = (
        f"[0:v]split=2"
        f"[original][blur];"

        f"[blur]"
        f"crop={w}:{h}:{x}:{y},"
        f"boxblur=18:3"
        f"[blurred];"

        f"[original][blurred]"
        f"overlay={x}:{y}"
        f"[clean];"

        f"[clean]"
        f"subtitles='{srt_path}'"
        f":force_style='"
        f"FontName=Arial,"
        f"FontSize=22,"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00101010,"
        f"BorderStyle=1,"
        f"Outline=2,"
        f"Shadow=1,"
        f"MarginV=25'"
        f"[video]"
    )

    if has_audio:
        fc = (
            video_filter
            + ";"
            "[0:a]volume=0.18"
            "[original_audio];"
            "[original_audio][1:a]"
            "amix=inputs=2:"
            "duration=first:"
            "dropout_transition=2"
            "[audio]"
        )

        maps = [
            "-map", "[video]",
            "-map", "[audio]"
        ]

    else:
        fc = video_filter

        maps = [
            "-map", "[video]",
            "-map", "1:a:0"
        ]

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", inp,
            "-i", str(voice_track),
            "-filter_complex", fc,
            *maps,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            out
        ],
        check=True
    )


def write_srt(path, segments):
    with open(
        path,
        "w",
        encoding="utf-8-sig"
    ) as f:

        for number, seg in enumerate(
            segments,
            1
        ):
            start = max(
                0,
                seg["start"]
            )

            end = max(
                start + 0.3,
                seg["end"]
            )

            f.write(
                f"{number}\n"
                f"{ts(start)} --> {ts(end)}\n"
                f"{seg['vi']}\n\n"
            )


def ffmpeg_path(path):
    value = str(
        Path(path).resolve()
    ).replace("\\", "/")

    return (
        value
        .replace(":", r"\:")
        .replace("'", r"\'")
    )


def ts(seconds):
    ms = max(
        0,
        int(seconds * 1000)
    )

    hours, ms = divmod(
        ms,
        3600000
    )

    minutes, ms = divmod(
        ms,
        60000
    )

    secs, ms = divmod(
        ms,
        1000
    )

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{secs:02},"
        f"{ms:03}"
    )
