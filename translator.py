import os
import json
import base64
import subprocess
import tempfile
import re
from pathlib import Path

from openai import OpenAI


def process(inp, out, cb, region):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        cb(15, "Đang đọc kích thước video…")
        width, height, duration = probe_video(inp)

        # =========================
        # TÍNH VÙNG PHỤ ĐỀ
        # =========================
        x = max(0, min(width - 1, int(width * region["x"])))
        y = max(0, min(height - 1, int(height * region["y"])))
        w = max(2, min(width - x, int(width * region["w"])))
        h = max(2, min(height - y, int(height * region["h"])))

        if w < 10 or h < 10:
            raise RuntimeError("Vùng phụ đề quá nhỏ.")

        # =========================
        # TÁCH FRAME ĐỂ OCR
        # =========================
        sample_step = 1.0
        max_duration = min(duration, 600.0)

        frame_dir = td / "frames"
        frame_dir.mkdir()

        cb(20, "Đang quét chữ Trung trong vùng đã chọn…")

        total = max(1, int(max_duration / sample_step))

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                inp,
                "-vf",
                f"fps=1/{sample_step},crop={w}:{h}:{x}:{y},scale='min(1280,iw)':-2",
                "-frames:v",
                str(total),
                str(frame_dir / "%05d.jpg"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        raw = []
        frames = sorted(frame_dir.glob("*.jpg"))

        # =========================
        # OCR
        # =========================
        for idx, frame in enumerate(frames):
            t = idx * sample_step

            text = ocr_frame(client, frame)

            if text:
                text = clean_text(text)

                if text:
                    raw.append({
                        "start": t,
                        "text": text
                    })

            cb(
                20 + int(
                    35 * (idx + 1) / max(1, len(frames))
                ),
                f"OCR phụ đề… {idx + 1}/{len(frames)}"
            )

        segments = group_ocr(raw)

        if not segments:
            raise RuntimeError(
                "Không nhận diện được chữ Trung trong vùng đã chọn."
            )

        # =========================
        # DỊCH
        # =========================
        cb(58, "Đang dịch phụ đề theo ngữ cảnh…")

        translated = translate_segments(
            client,
            segments,
            cb
        )

        if not translated:
            raise RuntimeError("Không có phụ đề để dịch.")

        # =========================
        # SRT
        # =========================
        srt = td / "vietsub.srt"

        write_srt(
            srt,
            translated
        )

        # =========================
        # TẠO GIỌNG LỒNG TIẾNG
        # =========================
        cb(84, "Đang tạo giọng lồng tiếng Việt…")

        voice_dir = td / "voices"
        voice_dir.mkdir()

        create_voice_track(
            client,
            translated,
            voice_dir,
            td,
            duration,
            cb
        )

        voice_track = td / "voice_track.wav"

        if not voice_track.exists():
            raise RuntimeError(
                "Không tạo được track lồng tiếng."
            )

        # =========================
        # CHE PHỤ ĐỀ + CHÈN VIỆT
        # =========================
        cb(
            94,
            "Đang che phụ đề cũ và ghép tiếng Việt…"
        )

        srt_path = (
            str(srt.resolve())
            .replace("\\", "/")
            .replace(":", "\\:")
        )

        # Blur đúng vùng phụ đề cũ.
        #
        # Đồng thời:
        # - giảm âm thanh gốc
        # - thêm giọng Việt
        #
        filter_complex = (
            f"[0:v]split=2[base][blur];"
            f"[blur]crop={w}:{h}:{x}:{y},"
            f"boxblur=12:2[blurred];"
            f"[base][blurred]overlay={x}:{y}[clean];"
            f"[clean]subtitles='{srt_path}':"
            f"force_style='"
            f"FontName=Arial,"
            f"FontSize=22,"
            f"PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00101010,"
            f"BorderStyle=1,"
            f"Outline=2,"
            f"Shadow=1,"
            f"MarginV=20'[vout];"
            f"[0:a]volume=0.18[orig];"
            f"[orig][1:a]amix="
            f"inputs=2:"
            f"duration=first:"
            f"dropout_transition=2[aout]"
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
                "[vout]",

                "-map",
                "[aout]",

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "22",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-movflags",
                "+faststart",

                out,
            ],
            check=True,
        )

        cb(
            100,
            "🎉 Hoàn tất! Video đã được lồng tiếng Việt."
        )


# =========================================================
# VIDEO INFO
# =========================================================

def probe_video(path):
    r = subprocess.run(
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
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    data = json.loads(r.stdout)["streams"][0]

    return (
        int(data["width"]),
        int(data["height"]),
        float(data.get("duration") or 0),
    )


# =========================================================
# OCR
# =========================================================

def ocr_frame(client, frame):
    data = base64.b64encode(
        frame.read_bytes()
    ).decode("utf-8")

    prompt = (
        "Read ONLY the visible Chinese subtitle text "
        "in this image. "
        "Ignore people, faces, logos, UI, watermarks "
        "and scenery. "
        "Return plain text only. "
        "If there is no Chinese subtitle, return EMPTY."
    )

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                "data:image/jpeg;base64,"
                                + data
                            ),
                            "detail": "low",
                        },
                    },
                ],
            }
        ],
    )

    return (
        r.choices[0].message.content or ""
    ).strip()


# =========================================================
# CLEAN OCR
# =========================================================

def clean_text(s):
    s = re.sub(
        r"\s+",
        " ",
        s
    ).strip()

    if s.lower() in {
        "empty",
        "none",
        "no subtitle",
        "không có",
    }:
        return ""

    return s


# =========================================================
# GỘP OCR
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
                    "end": item["start"] + 1.2,
                    "zh": item["text"],
                }
            )
            continue

        last = result[-1]

        # Cùng câu xuất hiện liên tục
        if (
            item["text"] == last["zh"]
            and item["start"] - last["end"] <= 1.5
        ):
            last["end"] = item["start"] + 1.2

        else:
            last["end"] = max(
                last["end"],
                item["start"]
            )

            result.append(
                {
                    "start": item["start"],
                    "end": item["start"] + 1.2,
                    "zh": item["text"],
                }
            )

    # Không để đoạn vượt video
    for item in result:
        if item["end"] <= item["start"]:
            item["end"] = item["start"] + 1.0

    return result


# =========================================================
# DỊCH
# =========================================================

def translate_segments(client, segments, cb):
    vals = []

    # Mỗi request 20 câu để giảm số lần gọi API
    batch_size = 20

    for start in range(
        0,
        len(segments),
        batch_size,
    ):
        chunk = segments[
            start:start + batch_size
        ]

        context = segments[
            max(0, start - 6):
            min(
                len(segments),
                start + batch_size + 6,
            )
        ]

        prompt = (
            "Dịch phụ đề tiếng Trung sang "
            "tiếng Việt tự nhiên và dễ nghe "
            "khi lồng tiếng. "
            "Dựa vào ngữ cảnh xung quanh. "
            "Giữ tên riêng nhất quán. "
            "Không dịch quá dài. "
            "Chỉ trả JSON dạng "
            "{\"translations\":[...]}, "
            "đúng số lượng câu.\n\n"

            "CONTEXT:\n"
            + "\n".join(
                x["zh"] for x in context
            )

            + "\n\nTARGET:\n"

            + "\n".join(
                f"{i + 1}. {x['zh']}"
                for i, x in enumerate(chunk)
            )
        )

        r = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={
                "type": "json_object"
            },
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là biên dịch viên "
                        "Trung-Việt chuyên nghiệp "
                        "và chuyên làm phụ đề lồng tiếng."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )

        data = json.loads(
            r.choices[0].message.content
        )

        tr = data.get("translations")

        if (
            not isinstance(tr, list)
            or len(tr) != len(chunk)
        ):
            raise RuntimeError(
                "AI trả về số lượng phụ đề không hợp lệ."
            )

        for seg, vi in zip(chunk, tr):
            vals.append(
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "vi": str(vi).strip(),
                }
            )

        cb(
            58
            + int(
                25
                * min(
                    start + batch_size,
                    len(segments),
                )
                / len(segments)
            ),
            (
                f"Đang dịch… "
                f"{min(start + batch_size, len(segments))}"
                f"/{len(segments)}"
            ),
        )

    return vals


# =========================================================
# TẠO GIỌNG VIỆT
# =========================================================

def create_voice_track(
    client,
    segments,
    voice_dir,
    td,
    duration,
    cb,
):
    voice_files = []

    # Có thể đổi bằng Environment Variable:
    #
    # TTS_VOICE=alloy
    #
    # Nếu không đặt thì dùng alloy.
    voice = os.environ.get(
        "TTS_VOICE",
        "alloy"
    )

    total = len(segments)

    for index, seg in enumerate(segments):

        text = seg["vi"].strip()

        if not text:
            continue

        output = voice_dir / (
            f"voice_{index:05d}.mp3"
        )

        # Tạo giọng nói
        response = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=text,
            response_format="mp3",
        )

        response.write_to_file(
            str(output)
        )

        voice_files.append(
            (
                index,
                seg,
                output,
            )
        )

        cb(
            84
            + int(
                7
                * (index + 1)
                / max(1, total)
            ),
            (
                f"Đang tạo giọng… "
                f"{index + 1}/{total}"
            ),
        )

    if not voice_files:
        raise RuntimeError(
            "Không tạo được đoạn giọng Việt nào."
        )

    # =====================================================
    # TẠO TRACK AUDIO IM LẶNG BẰNG FFmpeg
    # =====================================================

    voice_track = td / "voice_track.wav"

    inputs = []
    filters = []

    # Tạo một track silence đúng độ dài video
    #
    # Sau đó delay từng đoạn voice
    #
    # Ví dụ:
    # đoạn 1 bắt đầu 3 giây
    # -> adelay=3000
    #
    for i, (index, seg, audio) in enumerate(
        voice_files
    ):

        inputs.extend(
            [
                "-i",
                str(audio),
            ]
        )

        start_ms = max(
            0,
            int(seg["start"] * 1000)
        )

        filters.append(
            f"[{i}:a]"
            f"adelay={start_ms}:all=1"
            f"[v{i}]"
        )

    mix_inputs = "".join(
        f"[v{i}]"
        for i in range(len(voice_files))
    )

    filters.append(
        f"{mix_inputs}"
        f"amix="
        f"inputs={len(voice_files)}:"
        f"duration=longest:"
        f"dropout_transition=0,"
        f"aresample=async=1"
        f"[mixed]"
    )

    filter_text = ";".join(filters)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            filter_text,
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
            str(voice_track),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # =====================================================
    # CĂN CHO TRACK CÓ ĐÚNG ĐỘ DÀI VIDEO
    # =====================================================

    fixed_track = td / "voice_track_fixed.wav"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(voice_track),
            "-af",
            (
                "apad,"
                f"atrim=0:{duration}"
            ),
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(fixed_track),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    fixed_track.replace(
        voice_track
    )


# =========================================================
# SRT
# =========================================================

def write_srt(path, segments):
    with open(
        path,
        "w",
        encoding="utf-8-sig"
    ) as f:

        for i, s in enumerate(
            segments,
            1
        ):
            f.write(
                f"{i}\n"
                f"{ts(s['start'])} --> "
                f"{ts(s['end'])}\n"
                f"{s['vi']}\n\n"
            )


# =========================================================
# TIMESTAMP
# =========================================================

def ts(x):
    ms = max(
        0,
        int(x * 1000)
    )

    h, ms = divmod(
        ms,
        3600000
    )

    m, ms = divmod(
        ms,
        60000
    )

    s, ms = divmod(
        ms,
        1000
    )

    return (
        f"{h:02}:"
        f"{m:02}:"
        f"{s:02},"
        f"{ms:03}"
    )
