```python
import os
import json
import subprocess
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI


# =========================
# CONFIG
# =========================

STT_MODEL = os.environ.get(
    "STT_MODEL",
    "gpt-4o-transcribe"
)

TEXT_MODEL = os.environ.get(
    "TEXT_MODEL",
    "gpt-4o-mini"
)

TTS_MODEL = os.environ.get(
    "TTS_MODEL",
    "gpt-4o-mini-tts"
)

TTS_VOICE = os.environ.get(
    "TTS_VOICE",
    "alloy"
)

MAX_VIDEO_SECONDS = int(
    os.environ.get(
        "MAX_VIDEO_SECONDS",
        "600"
    )
)


# =========================
# MAIN PROCESS
# =========================

def process(inp, out, cb, region):
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        max_retries=2
    )

    with tempfile.TemporaryDirectory() as temp:
        td = Path(temp)

        # ---------------------------------
        # 1. ANALYZE VIDEO
        # ---------------------------------

        cb(7, "Đang phân tích video…")

        width, height, duration = probe_video(inp)

        if duration > MAX_VIDEO_SECONDS:
            duration = MAX_VIDEO_SECONDS

        # Region vẫn được giữ lại để CHE phụ đề cũ.
        # KHÔNG dùng region để nhận diện nội dung nữa.

        x = max(
            0,
            min(
                int(width * float(region.get("x", 0))),
                width - 2
            )
        )

        y = max(
            0,
            min(
                int(height * float(region.get("y", 0))),
                height - 2
            )
        )

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

        # ---------------------------------
        # 2. EXTRACT AUDIO
        # ---------------------------------

        cb(
            12,
            "Đang tách âm thanh tiếng Trung…"
        )

        audio_file = td / "source_audio.mp3"

        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                inp,
                "-t",
                str(duration),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(audio_file)
            ],
            check=True
        )

        if not audio_file.exists():
            raise RuntimeError(
                "Không thể tách âm thanh khỏi video."
            )

        # ---------------------------------
        # 3. SPEECH TO TEXT
        # ---------------------------------

        cb(
            20,
            "Đang nghe và nhận diện tiếng Trung…"
        )

        segments = transcribe_audio(
            client,
            audio_file,
            cb
        )

        if not segments:
            raise RuntimeError(
                "Không nhận diện được lời thoại tiếng Trung trong video."
            )

        # ---------------------------------
        # 4. TRANSLATE
        # ---------------------------------

        cb(
            48,
            "Đang dịch lời thoại sang tiếng Việt…"
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

        # ---------------------------------
        # 5. SRT
        # ---------------------------------

        srt = td / "vietsub.srt"

        write_srt(
            srt,
            translated
        )

        # ---------------------------------
        # 6. TTS
        # ---------------------------------

        cb(
            72,
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

        # ---------------------------------
        # 7. RENDER
        # ---------------------------------

        cb(
            93,
            "Đang che phụ đề cũ và hoàn thiện video…"
        )

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


# =========================
# VIDEO PROBE
# =========================

def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,duration:"
            "format=duration",
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

    width = int(
        stream.get("width") or 0
    )

    height = int(
        stream.get("height") or 0
    )

    duration = float(
        stream.get("duration") or 0
    )

    if duration <= 0:
        duration = float(
            data.get("format", {})
            .get("duration") or 0
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


# =========================
# SPEECH TO TEXT
# =========================

def transcribe_audio(client, audio_file, cb):
    """
    Nghe trực tiếp audio tiếng Trung.

    Không OCR.
    Không cần phụ đề Trung.
    """

    try:
        with open(
            audio_file,
            "rb"
        ) as f:

            transcript = client.audio.transcriptions.create(
                model=STT_MODEL,
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
                language="zh",
                temperature=0
            )

    except Exception as e:
        raise RuntimeError(
            f"Lỗi nhận diện giọng nói: {e}"
        )

    segments = getattr(
        transcript,
        "segments",
        None
    )

    if not segments:
        return []

    result = []

    total = len(segments)

    for i, segment in enumerate(
        segments
    ):
        text = getattr(
            segment,
            "text",
            ""
        )

        start = getattr(
            segment,
            "start",
            None
        )

        end = getattr(
            segment,
            "end",
            None
        )

        if not text:
            continue

        if start is None or end is None:
            continue

        text = str(text).strip()

        if not text:
            continue

        # Chỉ giữ những đoạn có chữ Trung.
        if not contains_chinese(text):
            continue

        result.append(
            {
                "start": float(start),
                "end": float(end),
                "zh": text
            }
        )

        cb(
            20 + int(
                25 * (i + 1) /
                max(1, total)
            ),
            f"Đang nhận diện lời thoại… "
            f"{i + 1}/{total}"
        )

    return merge_segments(result)


def contains_chinese(text):
    return any(
        "\u3400" <= ch <= "\u9fff"
        for ch in text
    )


# =========================
# MERGE STT SEGMENTS
# =========================

def merge_segments(segments):
    if not segments:
        return []

    result = []

    for seg in segments:
        text = seg["zh"].strip()

        if not text:
            continue

        if not result:
            result.append(
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "zh": text
                }
            )
            continue

        last = result[-1]

        # Nếu STT chia cùng câu thành nhiều đoạn
        # và khoảng nghỉ rất ngắn thì gộp.
        gap = (
            seg["start"] -
            last["end"]
        )

        if gap <= 0.35:
            last["end"] = max(
                last["end"],
                seg["end"]
            )

            last["zh"] += " " + text

        else:
            result.append(
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "zh": text
                }
            )

    return result


# =========================
# TRANSLATION
# =========================

def translate_segments(
    client,
    segments,
    cb
):
    results = []

    batch_size = 30
    total = len(segments)

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
            max(0, start - 5):
            min(
                total,
                start + batch_size + 5
            )
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
Bạn là biên dịch viên Trung-Việt chuyên nghiệp.

Dịch lời thoại tiếng Trung sang tiếng Việt tự nhiên
để LỒNG TIẾNG vào video.

YÊU CẦU:
- Dịch đúng nghĩa.
- Dịch tự nhiên như người Việt nói.
- Câu ngắn gọn, dễ đọc thành tiếng.
- Giữ tên nhân vật nhất quán.
- Không giải thích.
- Không thêm nội dung.
- Không bỏ câu.
- Giữ đúng thứ tự.

CONTEXT:
{context_text}

CẦN DỊCH:
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
                        "Bạn chuyên dịch Trung-Việt "
                        "cho video lồng tiếng."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )

        data = json.loads(
            response.choices[0]
            .message.content
        )

        translations = data.get(
            "translations"
        )

        if (
            not isinstance(
                translations,
                list
            )
            or len(translations)
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
                    "start": seg["start"],
                    "end": seg["end"],
                    "vi": str(vi).strip()
                }
            )

        completed = min(
            start + batch_size,
            total
        )

        cb(
            48 + int(
                24 * completed /
                total
            ),
            f"Đang dịch… "
            f"{completed}/{total}"
        )

    return results


# =========================
# TTS
# =========================

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

    def make_one(item):
        i, seg = item

        text = seg["vi"].strip()

        if not text:
            return None

        filename = (
            voice_dir /
            f"voice_{i:05d}.mp3"
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
            for item in enumerate(
                segments
            )
        ]

        for future in as_completed(
            futures
        ):
            item = future.result()

            completed += 1

            if item:
                audio_files.append(item)

            cb(
                72 + int(
                    19 *
                    completed /
                    max(1, total)
                ),
                f"Đang tạo giọng Việt… "
                f"{completed}/{total}"
            )

    audio_files.sort(
        key=lambda item:
        item[0]["start"]
    )

    if not audio_files:
        raise RuntimeError(
            "Không tạo được giọng Việt."
        )

    inputs = []
    filters = []

    for index, (
        seg,
        audio
    ) in enumerate(
        audio_files
    ):
        inputs += [
            "-i",
            str(audio)
        ]

        delay = max(
            0,
            int(
                seg["start"] * 1000
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

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
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
        check=True
    )


# =========================
# VIDEO RENDER
# =========================

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
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            inp
        ],
        capture_output=True,
        text=True
    )

    has_audio = bool(
        audio_check.stdout.strip()
    )

    # Làm mờ/che đúng vùng subtitle cũ.
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
            "[0:a]"
            "volume=0.18"
            "[original_audio];"
            "[original_audio][1:a]"
            "amix="
            "inputs=2:"
            "duration=first:"
            "dropout_transition=2"
            "[audio]"
        )

        maps = [
            "-map",
            "[video]",
            "-map",
            "[audio]"
        ]

    else:
        fc = video_filter

        maps = [
            "-map",
            "[video]",
            "-map",
            "1:a:0"
        ]

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            inp,
            "-i",
            str(voice_track),
            "-filter_complex",
            fc,
            *maps,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            out
        ],
        check=True
    )


# =========================
# SRT
# =========================

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


# =========================
# HELPERS
# =========================

def ffmpeg_path(path):
    value = str(
        Path(path).resolve()
    ).replace(
        "\\",
        "/"
    )

    return (
        value
        .replace(
            ":",
            r"\:"
        )
        .replace(
            "'",
            r"\'"
        )
    )


def ts(seconds):
    ms = max(
        0,
        int(
            seconds * 1000
        )
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
```
