import os
import json
import base64
import subprocess
import tempfile
import re
from pathlib import Path

from openai import OpenAI


# =========================================================
# MAIN
# =========================================================

def process(inp, out, cb, region):

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"]
    )

    with tempfile.TemporaryDirectory() as temp:

        td = Path(temp)

        # -------------------------------------------------
        # VIDEO INFO
        # -------------------------------------------------

        cb(
            10,
            "Đang phân tích video…"
        )

        width, height, duration = probe_video(inp)

        x = int(width * region["x"])
        y = int(height * region["y"])
        w = int(width * region["w"])
        h = int(height * region["h"])

        x = max(0, min(x, width - 2))
        y = max(0, min(y, height - 2))
        w = max(10, min(w, width - x))
        h = max(10, min(h, height - y))

        # -------------------------------------------------
        # EXTRACT FRAMES
        # -------------------------------------------------

        cb(
            15,
            "Đang quét vùng phụ đề…"
        )

        frames_dir = td / "frames"
        frames_dir.mkdir()

        # OCR mỗi 1.2 giây để giảm số request
        step = 1.2

        max_duration = min(
            duration,
            600
        )

        frame_count = max(
            1,
            int(max_duration / step)
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                inp,
                "-vf",
                (
                    f"fps=1/{step},"
                    f"crop={w}:{h}:{x}:{y},"
                    f"scale='min(1280,iw)':-2"
                ),
                "-frames:v",
                str(frame_count),
                str(frames_dir / "%05d.jpg")
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        frames = sorted(
            frames_dir.glob("*.jpg")
        )

        if not frames:
            raise RuntimeError(
                "Không thể đọc frame của video."
            )

        # -------------------------------------------------
        # OCR
        # -------------------------------------------------

        raw = []

        for i, frame in enumerate(frames):

            timestamp = i * step

            try:

                text = ocr_frame(
                    client,
                    frame
                )

                text = clean_text(
                    text
                )

                if text:
                    raw.append(
                        {
                            "start": timestamp,
                            "text": text
                        }
                    )

            except Exception:
                # Một frame OCR lỗi thì bỏ qua,
                # không làm hỏng cả video.
                pass

            cb(
                15 + int(
                    35 *
                    (i + 1) /
                    len(frames)
                ),
                (
                    "Đang nhận diện phụ đề… "
                    f"{i + 1}/{len(frames)}"
                )
            )

        segments = group_ocr(
            raw
        )

        if not segments:
            raise RuntimeError(
                "Không tìm thấy phụ đề Trung trong vùng đã chọn."
            )

        # -------------------------------------------------
        # TRANSLATE
        # -------------------------------------------------

        cb(
            52,
            "Đang dịch sang tiếng Việt…"
        )

        translated = translate_segments(
            client,
            segments,
            cb
        )

        if not translated:
            raise RuntimeError(
                "Không có nội dung để dịch."
            )

        # -------------------------------------------------
        # SRT
        # -------------------------------------------------

        srt = td / "vietsub.srt"

        write_srt(
            srt,
            translated
        )

        # -------------------------------------------------
        # TTS
        # -------------------------------------------------

        cb(
            80,
            "Đang tạo giọng lồng tiếng Việt…"
        )

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

        # -------------------------------------------------
        # FINAL VIDEO
        # -------------------------------------------------

        cb(
            94,
            "Đang che phụ đề cũ và hoàn thiện video…"
        )

        srt_path = ffmpeg_path(
            srt
        )

        # Blur vùng phụ đề cũ
        #
        # Âm thanh:
        # tiếng gốc = 18%
        # tiếng Việt = 100%
        #
        filter_complex = (
            f"[0:v]split=2[original][blur];"

            f"[blur]"
            f"crop={w}:{h}:{x}:{y},"
            f"boxblur=18:3"
            f"[blurred];"

            f"[original][blurred]"
            f"overlay={x}:{y}"
            f"[clean];"

            f"[clean]"
            f"subtitles='{srt_path}':"
            f"force_style='"
            f"FontName=Arial,"
            f"FontSize=22,"
            f"PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00101010,"
            f"BorderStyle=1,"
            f"Outline=2,"
            f"Shadow=1,"
            f"MarginV=25'"
            f"[video];"

            f"[0:a]"
            f"volume=0.18"
            f"[original_audio];"

            f"[original_audio][1:a]"
            f"amix="
            f"inputs=2:"
            f"duration=first:"
            f"dropout_transition=2"
            f"[audio]"
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",

                "-i",
                inp,

                "-i",
                str(voice_track),

                "-filter_complex",
                filter_complex,

                "-map",
                "[video]",

                "-map",
                "[audio]",

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "23",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-movflags",
                "+faststart",

                out
            ],
            check=True
        )

        cb(
            100,
            "🎉 Hoàn tất! Video đã được dịch và lồng tiếng."
        )


# =========================================================
# VIDEO PROBE
# =========================================================

def probe_video(path):

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration",
            "-of",
            "json",
            path
        ],
        check=True,
        capture_output=True,
        text=True
    )

    data = json.loads(
        result.stdout
    )

    stream = data["streams"][0]

    width = int(
        stream["width"]
    )

    height = int(
        stream["height"]
    )

    duration = float(
        stream.get("duration") or 0
    )

    return (
        width,
        height,
        duration
    )


# =========================================================
# OCR
# =========================================================

def ocr_frame(client, frame):

    image_data = base64.b64encode(
        frame.read_bytes()
    ).decode(
        "utf-8"
    )

    prompt = """
Đọc CHỈ phần phụ đề tiếng Trung
đang hiển thị trong ảnh.

Không đọc:
- người
- khuôn mặt
- logo
- watermark
- giao diện
- cảnh vật
- chữ trang trí

Nếu không có phụ đề tiếng Trung,
chỉ trả về EMPTY.

Chỉ trả về nội dung chữ.
Không giải thích.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
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
                            "detail": "low"
                        }
                    }
                ]
            }
        ]
    )

    return (
        response
        .choices[0]
        .message
        .content
        or ""
    ).strip()


# =========================================================
# CLEAN OCR
# =========================================================

def clean_text(text):

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    if not text:
        return ""

    lower = text.lower()

    invalid = {
        "empty",
        "none",
        "no subtitle",
        "no chinese subtitle",
        "không có",
        "không có phụ đề"
    }

    if lower in invalid:
        return ""

    # Phải có ít nhất một ký tự Trung
    if not re.search(
        r"[\u3400-\u4dbf\u4e00-\u9fff]",
        text
    ):
        return ""

    return text


# =========================================================
# GROUP OCR
# =========================================================

def group_ocr(raw):

    if not raw:
        return []

    result = []

    for item in raw:

        if not result:

            result.append(
                {
                    "start": item["start"],
                    "end":
                        item["start"] + 1.5,
                    "zh": item["text"]
                }
            )

            continue

        last = result[-1]

        # Cùng một câu
        if (
            item["text"] == last["zh"]
            and
            item["start"] -
            last["end"] <= 1.8
        ):

            last["end"] = (
                item["start"] + 1.5
            )

        else:

            # Nếu câu trước kéo dài
            last["end"] = max(
                last["end"],
                item["start"]
            )

            result.append(
                {
                    "start":
                        item["start"],

                    "end":
                        item["start"] + 1.5,

                    "zh":
                        item["text"]
                }
            )

    return result


# =========================================================
# TRANSLATION
# =========================================================

def translate_segments(
    client,
    segments,
    cb
):

    results = []

    batch_size = 20

    total = len(
        segments
    )

    for start in range(
        0,
        total,
        batch_size
    ):

        chunk = segments[
            start:
            start + batch_size
        ]

        context = segments[
            max(0, start - 6):
            min(
                total,
                start + batch_size + 6
            )
        ]

        context_text = "\n".join(
            x["zh"]
            for x in context
        )

        target_text = "\n".join(
            f"{i + 1}. {x['zh']}"
            for i, x
            in enumerate(chunk)
        )

        prompt = f"""
Dịch phụ đề tiếng Trung sang
tiếng Việt tự nhiên.

Mục đích cuối cùng là LỒNG TIẾNG,
nên câu tiếng Việt phải:
- tự nhiên khi nói
- ngắn gọn
- đúng ngữ cảnh
- dễ nghe
- giữ tên nhân vật nhất quán
- không thêm giải thích

CONTEXT:
{context_text}

TARGET:
{target_text}

Chỉ trả JSON:

{{
  "translations": [
    "..."
  ]
}}

Phải có đúng {len(chunk)} câu.
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,

            response_format={
                "type": "json_object"
            },

            messages=[
                {
                    "role":
                        "system",

                    "content":
                        "Bạn là biên dịch viên "
                        "Trung-Việt chuyên nghiệp "
                        "và chuyên viết lời thoại "
                        "cho lồng tiếng."
                },

                {
                    "role":
                        "user",

                    "content":
                        prompt
                }
            ]
        )

        data = json.loads(
            response
            .choices[0]
            .message
            .content
        )

        translations = (
            data.get(
                "translations"
            )
        )

        if (
            not isinstance(
                translations,
                list
            )
            or
            len(translations)
            != len(chunk)
        ):

            raise RuntimeError(
                "AI trả về số lượng câu dịch không hợp lệ."
            )

        for seg, vi in zip(
            chunk,
            translations
        ):

            results.append(
                {
                    "start":
                        seg["start"],

                    "end":
                        seg["end"],

                    "vi":
                        str(vi).strip()
                }
            )

        completed = min(
            start + batch_size,
            total
        )

        cb(
            55 + int(
                25 *
                completed /
                total
            ),

            (
                "Đang dịch… "
                f"{completed}/{total}"
            )
        )

    return results


# =========================================================
# TTS
# =========================================================

def create_voice_track(
    client,
    segments,
    voice_dir,
    output,
    duration,
    cb
):

    voice = os.environ.get(
        "TTS_VOICE",
        "alloy"
    )

    audio_files = []

    total = len(
        segments
    )

    for i, seg in enumerate(
        segments
    ):

        text = seg["vi"].strip()

        if not text:
            continue

        filename = voice_dir / (
            f"voice_{i:05d}.mp3"
        )

        response = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=text,
            response_format="mp3"
        )

        response.write_to_file(
            str(filename)
        )

        audio_files.append(
            (
                seg,
                filename
            )
        )

        cb(
            80 + int(
                9 *
                (i + 1) /
                max(1, total)
            ),

            (
                "Đang tạo giọng Việt… "
                f"{i + 1}/{total}"
            )
        )

    if not audio_files:
        raise RuntimeError(
            "Không tạo được giọng Việt."
        )

    # -----------------------------------------------------
    # BUILD FFMPEG AUDIO FILTER
    # -----------------------------------------------------

    inputs = []
    filters = []

    for index, (
        seg,
        audio
    ) in enumerate(audio_files):

        inputs.extend(
            [
                "-i",
                str(audio)
            ]
        )

        delay = max(
            0,
            int(
                seg["start"] *
                1000
            )
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
        f"amix="
        f"inputs={len(audio_files)}:"
        f"duration=longest:"
        f"dropout_transition=0"
        f"[mixed]"
    )

    filter_complex = ";".join(
        filters
    )

    subprocess.run(
        [
            "ffmpeg",
            "-y",

            *inputs,

            "-filter_complex",
            filter_complex,

            "-map",
            "[mixed]",

            "-t",
            str(duration),

            "-ar",
            "48000",

            "-ac",
            "2",

            "-c:a",
            "pcm_s16le",

            str(output)
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )


# =========================================================
# SRT
# =========================================================

def write_srt(
    path,
    segments
):

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
                f"{ts(start)} --> "
                f"{ts(end)}\n"
                f"{seg['vi']}\n\n"
            )


# =========================================================
# FFMPEG PATH
# =========================================================

def ffmpeg_path(path):

    value = str(
        Path(path).resolve()
    )

    # Linux
    value = value.replace(
        "\\",
        "/"
    )

    # Escape colon
    value = value.replace(
        ":",
        "\\:"
    )

    # Escape single quote
    value = value.replace(
        "'",
        "\\'"
    )

    return value


# =========================================================
# TIMESTAMP
# =========================================================

def ts(seconds):

    milliseconds = max(
        0,
        int(
            seconds * 1000
        )
    )

    hours, milliseconds = divmod(
        milliseconds,
        3600000
    )

    minutes, milliseconds = divmod(
        milliseconds,
        60000
    )

    secs, milliseconds = divmod(
        milliseconds,
        1000
    )

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{secs:02},"
        f"{milliseconds:03}"
    )
