import os
import json
import re
import time
import shutil
import subprocess
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI


# ============================================================
# CONFIG
# ============================================================

STT_MODEL = os.getenv("STT_MODEL", "gpt-4o-transcribe")
TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

MAX_VIDEO_SECONDS = max(
    30,
    int(os.getenv("MAX_VIDEO_SECONDS", "600"))
)

STT_CHUNK_SECONDS = max(
    30,
    int(os.getenv("STT_CHUNK_SECONDS", "180"))
)

# Overlap nhỏ để không cắt mất câu ở ranh giới.
STT_OVERLAP_SECONDS = max(
    0.0,
    min(
        float(os.getenv("STT_OVERLAP_SECONDS", "1.5")),
        5.0
    )
)

STT_WORKERS = max(
    1,
    min(
        int(os.getenv("STT_WORKERS", "2")),
        3
    )
)

TTS_WORKERS = max(
    1,
    min(
        int(os.getenv("TTS_WORKERS", "2")),
        3
    )
)

ORIGINAL_AUDIO_VOLUME = max(
    0.0,
    min(
        float(os.getenv("ORIGINAL_AUDIO_VOLUME", "0.10")),
        1.0
    )
)

STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 48000
MAX_RETRIES = 3


# ============================================================
# MAIN
# ============================================================

def process(inp, out, cb=None, region=None):
    cb = cb or (lambda progress, message: None)

    inp = Path(inp)
    out = Path(out)

    validate_input(inp)
    check_ffmpeg()

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY chưa được cấu hình trên Render."
        )

    client = OpenAI(
        api_key=api_key,
        max_retries=0
    )

    with tempfile.TemporaryDirectory(
        prefix="vietsub_v6_"
    ) as temp:

        td = Path(temp)

        # ====================================================
        # VIDEO INFO
        # ====================================================

        cb(3, "Đang kiểm tra video…")

        info = probe_video(inp)

        width = info["width"]
        height = info["height"]
        source_duration = info["duration"]
        has_audio = info["has_audio"]

        duration = min(
            source_duration,
            float(MAX_VIDEO_SECONDS)
        )

        if duration <= 0:
            raise RuntimeError(
                "Video có thời lượng không hợp lệ."
            )

        # ====================================================
        # REGION
        # ====================================================

        x, y, w, h = normalize_region(
            width,
            height,
            region
        )

        # ====================================================
        # AUDIO
        # ====================================================

        if not has_audio:
            raise RuntimeError(
                "Video không có luồng âm thanh. "
                "Không thể nhận diện tiếng Trung để dịch."
            )

        cb(8, "Đang tách âm thanh…")

        source_audio = (
            td / "source_audio.wav"
        )

        extract_audio(
            inp,
            source_audio,
            duration
        )

        validate_file(
            source_audio,
            "Không thể tách âm thanh khỏi video."
        )

        # ====================================================
        # CHUNKS
        # ====================================================

        cb(12, "Đang chia âm thanh…")

        chunks_dir = td / "chunks"
        chunks_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        chunks = split_audio(
            source_audio,
            chunks_dir,
            duration
        )

        if not chunks:
            raise RuntimeError(
                "Không tạo được audio chunk."
            )

        # ====================================================
        # STT
        # ====================================================

        cb(
            16,
            f"Đang nhận diện tiếng Trung… 0/{len(chunks)}"
        )

        segments = transcribe_parallel(
            client,
            chunks,
            duration,
            cb
        )

        if not segments:
            raise RuntimeError(
                "Không nhận diện được lời thoại tiếng Trung."
            )

        # ====================================================
        # CLEAN
        # ====================================================

        cb(43, "Đang xử lý lời thoại…")

        segments = clean_segments(
            segments
        )

        if not segments:
            raise RuntimeError(
                "Không còn lời thoại hợp lệ sau khi xử lý."
            )

        # ====================================================
        # TRANSLATE
        # ====================================================

        cb(46, "Đang dịch sang tiếng Việt…")

        translated = translate_parallel(
            client,
            segments,
            cb
        )

        if not translated:
            raise RuntimeError(
                "Không tạo được bản dịch."
            )

        # ====================================================
        # SRT
        # ====================================================

        srt_file = td / "vietsub.srt"

        write_srt(
            srt_file,
            translated
        )

        validate_file(
            srt_file,
            "Không tạo được file subtitle."
        )

        # ====================================================
        # TTS
        # ====================================================

        cb(70, "Đang tạo giọng Việt…")

        voice_dir = td / "voices"
        voice_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        voice_track = td / "voice.wav"

        create_voice_track(
            client,
            translated,
            voice_dir,
            voice_track,
            duration,
            cb
        )

        validate_file(
            voice_track,
            "Không tạo được giọng Việt."
        )

        # ====================================================
        # RENDER
        # ====================================================

        cb(
            94,
            "Đang che phụ đề cũ và hoàn thiện video…"
        )

        render_video(
            inp,
            out,
            srt_file,
            voice_track,
            x,
            y,
            w,
            h,
            duration
        )

        validate_output(
            out
        )

        cb(
            100,
            "🎉 Hoàn tất! Video đã được dịch và lồng tiếng."
        )

        return str(out)


# ============================================================
# INPUT
# ============================================================

def validate_input(path):
    if not path.exists():
        raise RuntimeError(
            f"Không tìm thấy file video đầu vào: {path}"
        )

    if not path.is_file():
        raise RuntimeError(
            f"Đường dẫn đầu vào không phải file: {path}"
        )

    if path.stat().st_size < 1024:
        raise RuntimeError(
            "File video đầu vào rỗng hoặc bị hỏng."
        )


def check_ffmpeg():
    missing = []

    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")

    if shutil.which("ffprobe") is None:
        missing.append("ffprobe")

    if missing:
        raise RuntimeError(
            "Render thiếu: "
            + ", ".join(missing)
        )


# ============================================================
# FFPROBE
# ============================================================

def probe_video(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path)
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        error = (
            result.stderr.strip()
            or "FFprobe không đọc được file."
        )

        raise RuntimeError(
            "FFprobe không đọc được video: "
            + error
        )

    try:
        data = json.loads(
            result.stdout
        )
    except Exception as exc:
        raise RuntimeError(
            f"FFprobe trả về dữ liệu không hợp lệ: {exc}"
        )

    streams = data.get(
        "streams",
        []
    )

    video_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
        ),
        None
    )

    if not video_stream:
        raise RuntimeError(
            "Không tìm thấy luồng video trong file đầu vào."
        )

    width = safe_int(
        video_stream.get("width"),
        0
    )

    height = safe_int(
        video_stream.get("height"),
        0
    )

    if width <= 0 or height <= 0:
        raise RuntimeError(
            "Không đọc được kích thước video."
        )

    duration = safe_float(
        video_stream.get("duration"),
        0
    )

    if duration <= 0:
        duration = safe_float(
            data.get("format", {}).get("duration"),
            0
        )

    if duration <= 0:
        # Một số file có duration ở stream dạng N/A.
        # Thử packet duration.
        result2 = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path)
            ],
            capture_output=True,
            text=True
        )

        duration = safe_float(
            result2.stdout.strip(),
            0
        )

    if duration <= 0:
        raise RuntimeError(
            "Không xác định được thời lượng video."
        )

    has_audio = any(
        stream.get("codec_type") == "audio"
        for stream in streams
    )

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": has_audio
    }


# ============================================================
# REGION
# ============================================================

def normalize_region(
    width,
    height,
    region
):
    region = region or {}

    rx = safe_float(
        region.get("x"),
        0
    )

    ry = safe_float(
        region.get("y"),
        0
    )

    rw = safe_float(
        region.get("w"),
        1
    )

    rh = safe_float(
        region.get("h"),
        0.25
    )

    rx = max(
        0.0,
        min(rx, 0.999)
    )

    ry = max(
        0.0,
        min(ry, 0.999)
    )

    rw = max(
        0.01,
        min(rw, 1.0 - rx)
    )

    rh = max(
        0.01,
        min(rh, 1.0 - ry)
    )

    x = even(
        int(width * rx)
    )

    y = even(
        int(height * ry)
    )

    w = even(
        int(width * rw)
    )

    h = even(
        int(height * rh)
    )

    x = max(
        0,
        min(x, max(0, width - 2))
    )

    y = max(
        0,
        min(y, max(0, height - 2))
    )

    w = max(
        2,
        min(w, width - x)
    )

    h = max(
        2,
        min(h, height - y)
    )

    return x, y, w, h


# ============================================================
# AUDIO
# ============================================================

def extract_audio(
    video,
    output,
    duration
):
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video),
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(STT_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output)
        ]
    )


# ============================================================
# CHUNKS
# ============================================================

def split_audio(
    audio,
    directory,
    duration
):
    chunks = []

    chunk_size = max(
        30,
        STT_CHUNK_SECONDS
    )

    overlap = min(
        STT_OVERLAP_SECONDS,
        chunk_size * 0.10
    )

    main_start = 0.0
    index = 0

    while main_start < duration - 0.01:

        main_end = min(
            duration,
            main_start + chunk_size
        )

        # Chunk thực tế có thêm phần overlap.
        chunk_start = max(
            0.0,
            main_start - overlap
        )

        chunk_end = main_end

        length = (
            chunk_end
            - chunk_start
        )

        if length <= 0:
            break

        output = (
            directory
            / f"chunk_{index:04d}.wav"
        )

        run_ffmpeg(
            [
                "-y",
                "-ss",
                f"{chunk_start:.3f}",
                "-i",
                str(audio),
                "-t",
                f"{length:.3f}",
                "-ac",
                "1",
                "-ar",
                str(STT_SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(output)
            ]
        )

        validate_file(
            output,
            f"Không tạo được audio chunk {index + 1}."
        )

        chunks.append(
            {
                "path": output,
                "index": index,
                "offset": chunk_start,
                "main_start": main_start,
                "main_end": main_end,
                "real_end": chunk_end
            }
        )

        if main_end >= duration:
            break

        main_start = main_end
        index += 1

    return chunks


# ============================================================
# STT
# ============================================================

def transcribe_parallel(
    client,
    chunks,
    duration,
    cb
):
    all_segments = []

    total = len(chunks)
    completed = 0

    with ThreadPoolExecutor(
        max_workers=STT_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                transcribe_one,
                client,
                chunk
            ): chunk
            for chunk in chunks
        }

        for future in as_completed(
            futures
        ):
            chunk = futures[future]

            try:
                result = future.result()
            except Exception as exc:
                raise RuntimeError(
                    f"Lỗi STT chunk "
                    f"{chunk['index'] + 1}: {exc}"
                )

            all_segments.extend(
                result
            )

            completed += 1

            cb(
                16 + int(
                    26 *
                    completed /
                    max(1, total)
                ),
                (
                    "Đang nhận diện tiếng Trung… "
                    f"{completed}/{total}"
                )
            )

    all_segments.sort(
        key=lambda item: (
            item["start"],
            item["end"]
        )
    )

    return remove_duplicates(
        all_segments
    )


def transcribe_one(
    client,
    chunk
):
    last_error = None

    for attempt in range(
        MAX_RETRIES
    ):
        try:
            with open(
                chunk["path"],
                "rb"
            ) as audio:

                response = (
                    client.audio.transcriptions.create(
                        model=STT_MODEL,
                        file=audio,
                        response_format="verbose_json",
                        timestamp_granularities=[
                            "segment"
                        ],
                        language="zh",
                        temperature=0
                    )
                )

            return parse_stt(
                response,
                chunk
            )

        except Exception as exc:
            last_error = exc

            text = str(
                exc
            ).lower()

            if is_auth_error(text):
                raise RuntimeError(
                    "OPENAI_API_KEY không hợp lệ. "
                    "Kiểm tra Render Environment."
                )

            if is_permanent_error(text):
                raise RuntimeError(
                    f"OpenAI từ chối audio: {exc}"
                )

            if attempt < MAX_RETRIES - 1:
                time.sleep(
                    retry_delay(
                        text,
                        attempt
                    )
                )

    raise RuntimeError(
        f"STT thất bại: {last_error}"
    )


def parse_stt(
    response,
    chunk
):
    raw_segments = getattr(
        response,
        "segments",
        None
    )

    if not raw_segments:
        return []

    output = []

    for item in raw_segments:

        text = str(
            getattr(
                item,
                "text",
                ""
            ) or ""
        ).strip()

        if not text:
            continue

        local_start = safe_float(
            getattr(
                item,
                "start",
                None
            ),
            -1
        )

        local_end = safe_float(
            getattr(
                item,
                "end",
                None
            ),
            -1
        )

        if (
            local_start < 0
            or local_end <= local_start
        ):
            continue

        global_start = (
            chunk["offset"]
            + local_start
        )

        global_end = (
            chunk["offset"]
            + local_end
        )

        # Chỉ giữ phần thuộc vùng chính của chunk.
        # Chunk đầu giữ toàn bộ.
        if chunk["index"] > 0:
            if global_end <= chunk["main_start"]:
                continue

            # Nếu câu bắt đầu trong phần overlap
            # nhưng kết thúc sau main_start,
            # giữ nó và để dedupe xử lý.
            global_start = max(
                global_start,
                chunk["main_start"]
            )

        global_end = min(
            global_end,
            chunk["main_end"]
        )

        if global_end <= global_start:
            continue

        text = clean_stt_text(
            text
        )

        if not text:
            continue

        if not contains_chinese(text):
            continue

        output.append(
            {
                "start": global_start,
                "end": global_end,
                "zh": text
            }
        )

    return output


# ============================================================
# DEDUP
# ============================================================

def remove_duplicates(
    segments
):
    if not segments:
        return []

    result = []

    for current in segments:

        current_norm = normalize_text(
            current["zh"]
        )

        duplicate = False

        for previous in reversed(
            result[-10:]
        ):
            distance = (
                current["start"]
                - previous["start"]
            )

            if distance > 4:
                break

            previous_norm = normalize_text(
                previous["zh"]
            )

            if (
                current_norm
                == previous_norm
            ):
                previous["end"] = max(
                    previous["end"],
                    current["end"]
                )

                duplicate = True
                break

            # Chỉ xử lý containment với câu tương đối dài.
            if (
                len(current_norm) >= 12
                and len(previous_norm) >= 12
                and abs(
                    current["start"]
                    - previous["start"]
                ) <= 2
                and (
                    current_norm in previous_norm
                    or previous_norm in current_norm
                )
            ):
                if len(current_norm) > len(
                    previous_norm
                ):
                    previous["zh"] = current[
                        "zh"
                    ]

                    previous["start"] = min(
                        previous["start"],
                        current["start"]
                    )

                    previous["end"] = max(
                        previous["end"],
                        current["end"]
                    )

                else:
                    previous["end"] = max(
                        previous["end"],
                        current["end"]
                    )

                duplicate = True
                break

        if not duplicate:
            result.append(
                current.copy()
            )

    return result


def normalize_text(text):
    return re.sub(
        r"\s+",
        "",
        str(text or "").lower()
    )


# ============================================================
# CLEAN
# ============================================================

def clean_segments(
    segments
):
    cleaned = []

    for item in segments:

        text = clean_stt_text(
            item.get("zh", "")
        )

        if not text:
            continue

        start = safe_float(
            item.get("start"),
            0
        )

        end = safe_float(
            item.get("end"),
            0
        )

        if end <= start:
            continue

        cleaned.append(
            {
                "start": start,
                "end": end,
                "zh": text
            }
        )

    cleaned.sort(
        key=lambda item: item["start"]
    )

    # Không merge bừa các câu.
    # Chỉ gộp đoạn cực sát nhau nếu câu trước chưa kết thúc.
    result = []

    for current in cleaned:

        if not result:
            result.append(
                current.copy()
            )
            continue

        previous = result[-1]

        gap = (
            current["start"]
            - previous["end"]
        )

        if (
            0 <= gap <= 0.10
            and same_sentence(
                previous["zh"]
            )
            and len(previous["zh"]) < 100
        ):
            previous["end"] = max(
                previous["end"],
                current["end"]
            )

            previous["zh"] += (
                " "
                + current["zh"]
            )

        else:
            result.append(
                current.copy()
            )

    return result


def clean_stt_text(text):
    text = str(
        text or ""
    ).strip()

    if not text:
        return ""

    for marker in (
        "[BLANK_AUDIO]",
        "[MUSIC]",
        "[music]",
        "(music)",
        "（音乐）",
        "[SILENCE]",
        "[silence]",
        "（静音）"
    ):
        text = text.replace(
            marker,
            ""
        )

    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    return text


def same_sentence(text):
    return not str(
        text or ""
    ).rstrip().endswith(
        (
            "。",
            "！",
            "？",
            "!",
            "?",
            "…",
            "；",
            ";"
        )
    )


def contains_chinese(text):
    return any(
        "\u3400"
        <= char
        <= "\u9fff"
        for char in str(text)
    )


# ============================================================
# TRANSLATION
# ============================================================

def translate_parallel(
    client,
    segments,
    cb
):
    batch_size = 40

    batches = []

    for start in range(
        0,
        len(segments),
        batch_size
    ):
        batches.append(
            (
                start,
                segments[
                    start:
                    start + batch_size
                ]
            )
        )

    completed = 0
    results = []

    # Hai request song song là mức hợp lý.
    workers = min(
        2,
        len(batches)
    )

    with ThreadPoolExecutor(
        max_workers=max(1, workers)
    ) as executor:

        futures = {
            executor.submit(
                translate_batch,
                client,
                batch
            ): start
            for start, batch in batches
        }

        finished = []

        for future in as_completed(
            futures
        ):
            start = futures[future]

            try:
                translated = future.result()
            except Exception as exc:
                raise RuntimeError(
                    f"Lỗi dịch batch: {exc}"
                )

            finished.append(
                (
                    start,
                    translated
                )
            )

            completed += 1

            cb(
                46 + int(
                    22 *
                    completed /
                    max(1, len(batches))
                ),
                (
                    "Đang dịch… "
                    f"{completed}/{len(batches)}"
                )
            )

    finished.sort(
        key=lambda item: item[0]
    )

    for _, batch in finished:
        results.extend(batch)

    return results


def translate_batch(
    client,
    segments
):
    target = "\n".join(
        f"{i + 1}. {item['zh']}"
        for i, item in enumerate(
            segments
        )
    )

    prompt = f"""
Dịch tiếng Trung sang tiếng Việt cho video lồng tiếng.

YÊU CẦU:
- Đúng nghĩa.
- Tự nhiên như người Việt nói.
- Ngắn gọn.
- Dễ đọc thành tiếng.
- Không giải thích.
- Không thêm nội dung.
- Không bỏ nội dung.
- Giữ đúng thứ tự.
- Mỗi câu input tương ứng đúng một output.
- Không đánh số trong câu dịch.

INPUT:
{target}

Chỉ trả JSON:
{{"translations":["..."]}}

Phải có đúng {len(segments)} phần tử.
"""

    last_error = None

    for attempt in range(
        MAX_RETRIES
    ):
        try:
            response = (
                client.chat.completions.create(
                    model=TEXT_MODEL,
                    temperature=0.1,
                    response_format={
                        "type": "json_object"
                    },
                    messages=[
                        {
                            "role": "system",
                            "content":
                                "Bạn là biên dịch viên "
                                "Trung-Việt chuyên nghiệp."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                )
            )

            content = (
                response.choices[0]
                .message.content
            )

            data = json.loads(
                content
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
                != len(segments)
            ):
                raise RuntimeError(
                    "AI trả về sai số lượng câu."
                )

            output = []

            for segment, translation in zip(
                segments,
                translations
            ):
                vi = str(
                    translation or ""
                ).strip()

                if not vi:
                    vi = segment["zh"]

                output.append(
                    {
                        "start": segment["start"],
                        "end": segment["end"],
                        "vi": vi
                    }
                )

            return output

        except Exception as exc:
            last_error = exc

            text = str(
                exc
            ).lower()

            if is_auth_error(text):
                raise RuntimeError(
                    "OPENAI_API_KEY không hợp lệ."
                )

            if attempt < MAX_RETRIES - 1:
                time.sleep(
                    retry_delay(
                        text,
                        attempt
                    )
                )

    raise RuntimeError(
        f"Dịch thất bại: {last_error}"
    )


# ============================================================
# TTS
# ============================================================

def create_voice_track(
    client,
    segments,
    voice_dir,
    output,
    duration,
    cb
):
    total = len(segments)

    if total == 0:
        raise RuntimeError(
            "Không có câu để tạo TTS."
        )

    completed = 0
    audio_files = []

    with ThreadPoolExecutor(
        max_workers=TTS_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                create_one_tts,
                client,
                index,
                segment,
                voice_dir
            ): index
            for index, segment in enumerate(
                segments
            )
        }

        for future in as_completed(
            futures
        ):
            index = futures[future]

            try:
                audio_files.append(
                    future.result()
                )
            except Exception as exc:
                raise RuntimeError(
                    f"TTS câu {index + 1} lỗi: {exc}"
                )

            completed += 1

            cb(
                70 + int(
                    20 *
                    completed /
                    max(1, total)
                ),
                (
                    "Đang tạo giọng Việt… "
                    f"{completed}/{total}"
                )
            )

    audio_files.sort(
        key=lambda item: item[0]["start"]
    )

    build_voice_track(
        audio_files,
        output,
        duration
    )


def create_one_tts(
    client,
    index,
    segment,
    voice_dir
):
    text = str(
        segment.get("vi", "")
    ).strip()

    if not text:
        raise RuntimeError(
            "Câu rỗng."
        )

    output = (
        voice_dir
        / f"voice_{index:05d}.mp3"
    )

    last_error = None

    for attempt in range(
        MAX_RETRIES
    ):
        try:
            response = (
                client.audio.speech.create(
                    model=TTS_MODEL,
                    voice=TTS_VOICE,
                    input=text,
                    response_format="mp3"
                )
            )

            response.write_to_file(
                str(output)
            )

            validate_file(
                output,
                "TTS không tạo được file."
            )

            return (
                segment,
                output
            )

        except Exception as exc:
            last_error = exc

            text_error = str(
                exc
            ).lower()

            if is_auth_error(
                text_error
            ):
                raise RuntimeError(
                    "OPENAI_API_KEY không hợp lệ."
                )

            if attempt < MAX_RETRIES - 1:
                time.sleep(
                    retry_delay(
                        text_error,
                        attempt
                    )
                )

    raise RuntimeError(
        str(last_error)
    )


def build_voice_track(
    audio_files,
    output,
    duration
):
    if not audio_files:
        raise RuntimeError(
            "Không có audio TTS."
        )

    inputs = []
    filters = []
    labels = []

    for i, (
        segment,
        audio
    ) in enumerate(
        audio_files
    ):

        inputs.extend(
            [
                "-i",
                str(audio)
            ]
        )

        delay = max(
            0,
            int(
                segment["start"] * 1000
            )
        )

        label = f"voice{i}"

        filters.append(
            f"[{i}:a]"
            f"adelay={delay}:all=1"
            f"[{label}]"
        )

        labels.append(
            f"[{label}]"
        )

    if len(labels) == 1:
        filters.append(
            f"{labels[0]}"
            "aresample=48000,"
            "aformat="
            "sample_rates=48000:"
            "channel_layouts=stereo"
            "[mixed]"
        )
    else:
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:"
            "duration=longest:"
            "dropout_transition=0:"
            "normalize=1,"
            "aresample=48000,"
            "aformat="
            "sample_rates=48000:"
            "channel_layouts=stereo"
            "[mixed]"
        )

    run_ffmpeg(
        [
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[mixed]",
            "-t",
            f"{duration:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output)
        ]
    )


# ============================================================
# RENDER
# ============================================================

def render_video(
    inp,
    out,
    srt,
    voice_track,
    x,
    y,
    w,
    h,
    duration
):
    srt_path = ffmpeg_subtitle_path(
        srt
    )

    video_filter = (
        f"[0:v]"
        f"split=2"
        f"[base][blur_source];"

        f"[blur_source]"
        f"crop={w}:{h}:{x}:{y},"
        f"boxblur=18:3"
        f"[blurred];"

        f"[base][blurred]"
        f"overlay={x}:{y}"
        f"[clean];"

        f"[clean]"
        f"subtitles={srt_path}:"
        f"force_style='"
        f"FontSize=22,"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00101010,"
        f"BorderStyle=1,"
        f"Outline=2,"
        f"Shadow=1,"
        f"MarginV=25'"
        f"[video]"
    )

    filter_complex = (
        video_filter
        + ";"
        "[0:a]"
        f"volume={ORIGINAL_AUDIO_VOLUME},"
        "aresample=48000"
        "[original];"
        "[original][1:a]"
        "amix=inputs=2:"
        "duration=first:"
        "dropout_transition=0:"
        "normalize=1"
        "[audio]"
    )

    run_ffmpeg(
        [
            "-y",
            "-i",
            str(inp),
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
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(out)
        ]
    )


# ============================================================
# SRT
# ============================================================

def write_srt(
    path,
    segments
):
    with open(
        path,
        "w",
        encoding="utf-8"
    ) as file:

        number = 1

        for segment in segments:

            start = max(
                0,
                safe_float(
                    segment.get("start"),
                    0
                )
            )

            end = max(
                start + 0.35,
                safe_float(
                    segment.get("end"),
                    start + 0.35
                )
            )

            text = str(
                segment.get("vi", "")
            ).strip()

            text = re.sub(
                r"[\r\n]+",
                " ",
                text
            )

            if not text:
                continue

            file.write(
                f"{number}\n"
                f"{timestamp(start)} --> "
                f"{timestamp(end)}\n"
                f"{text}\n\n"
            )

            number += 1


def timestamp(seconds):
    ms = max(
        0,
        int(
            round(
                seconds * 1000
            )
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


# ============================================================
# FFMPEG
# ============================================================

def run_ffmpeg(args):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error"
    ] + list(args)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        error = (
            result.stderr.strip()
            or "Không có thông tin lỗi."
        )

        raise RuntimeError(
            "FFmpeg lỗi: " + error
        )

    return result


def ffmpeg_subtitle_path(path):
    value = str(
        Path(path).resolve()
    ).replace(
        "\\",
        "/"
    )

    # Escape dành cho FFmpeg filter parser.
    value = value.replace(
        "'",
        r"\'"
    )

    value = value.replace(
        ":",
        r"\:"
    )

    return f"'{value}'"


# ============================================================
# OUTPUT
# ============================================================

def validate_output(path):
    validate_file(
        path,
        "Video đầu ra không được tạo."
    )

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path)
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Video đầu ra bị lỗi."
        )

    if "video" not in result.stdout:
        raise RuntimeError(
            "Video đầu ra không có luồng video."
        )


def validate_file(
    path,
    message
):
    path = Path(path)

    if (
        not path.exists()
        or not path.is_file()
        or path.stat().st_size < 1024
    ):
        raise RuntimeError(
            message
        )


# ============================================================
# ERROR HELPERS
# ============================================================

def is_auth_error(text):
    text = str(
        text or ""
    ).lower()

    return (
        "401" in text
        or "invalid_api_key" in text
        or "incorrect api key" in text
        or "authentication" in text
    )


def is_permanent_error(text):
    text = str(
        text or ""
    ).lower()

    return (
        "400" in text
        or "unsupported" in text
        or "invalid file" in text
        or "invalid_request" in text
        or "file too large" in text
    )


def retry_delay(
    text,
    attempt
):
    text = str(
        text or ""
    ).lower()

    if (
        "429" in text
        or "rate limit" in text
        or "too many requests" in text
    ):
        return min(
            10,
            2 ** attempt
        )

    if (
        "timeout" in text
        or "timed out" in text
    ):
        return min(
            8,
            2 + attempt * 2
        )

    return min(
        6,
        1.5 + attempt
    )


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_float(
    value,
    default=0.0
):
    try:
        return float(value)
    except Exception:
        return default


def safe_int(
    value,
    default=0
):
    try:
        return int(value)
    except Exception:
        return default


def even(value):
    return (
        max(2, int(value))
        // 2
        * 2
    )
