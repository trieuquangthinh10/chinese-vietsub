import os, subprocess, json
from pathlib import Path
from openai import OpenAI

def process(inp, out, cb):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    work = Path(out).with_suffix(".wav")
    cb(20, "Đang tách âm thanh…")
    subprocess.run(["ffmpeg","-y","-i",inp,"-vn","-ac","1","-ar","16000",str(work)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    cb(35, "Đang nhận diện tiếng Trung…")
    with open(work,"rb") as f:
        tr = client.audio.transcriptions.create(
            model="whisper-1", file=f, language="zh",
            response_format="verbose_json", timestamp_granularities=["segment"]
        )
    seg = [{"start":float(x.start),"end":float(x.end),"zh":x.text.strip()} for x in tr.segments if x.text.strip()]
    if not seg:
        raise RuntimeError("Không phát hiện được lời thoại tiếng Trung.")

    cb(50, "Đang dịch theo ngữ cảnh…")
    vals=[]
    for i in range(0,len(seg),12):
        chunk=seg[i:i+12]
        context=seg[max(0,i-8):min(len(seg),i+20)]
        prompt=("Dịch lời thoại tiếng Trung sang tiếng Việt tự nhiên. Dựa vào các câu xung quanh để "
                "hiểu ngữ cảnh, đại từ xưng hô, sắc thái, câu đùa và cách gọi nhân vật. "
                "Giữ tên riêng và thuật ngữ nhất quán. Chỉ trả JSON với key translations, đúng số lượng câu.\n"
                "CONTEXT:\n" + "\n".join(x["zh"] for x in context) +
                "\nTARGET:\n" + "\n".join(f"{k+1}. {x['zh']}" for k,x in enumerate(chunk)))
        r=client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.2,
            response_format={"type":"json_object"},
            messages=[{"role":"system","content":"Bạn là biên dịch viên phụ đề Trung-Việt chuyên nghiệp."},{"role":"user","content":prompt}]
        )
        data=json.loads(r.choices[0].message.content)
        trans=data.get("translations")
        if not isinstance(trans,list) or len(trans)!=len(chunk):
            raise RuntimeError("AI trả về định dạng dịch không hợp lệ.")
        vals += list(zip(chunk,trans))
        cb(50 + int(35*min(i+12,len(seg))/len(seg)), "Đang dịch…")

    srt=Path(out).with_suffix(".srt")
    with open(srt,"w",encoding="utf-8-sig") as f:
        for n,(x,t) in enumerate(vals,1):
            f.write(f"{n}\n{ts(x['start'])} --> {ts(x['end'])}\n{str(t).strip()}\n\n")

    cb(90, "Đang chèn phụ đề vào video…")
    esc=str(srt.resolve()).replace("\\","/").replace(":","\\:").replace("'","\\'")
    subprocess.run(["ffmpeg","-y","-i",inp,"-vf",f"subtitles='{esc}':force_style='FontName=Arial,FontSize=20,Outline=2,MarginV=35","-c:a","copy",out],check=True)
    try: work.unlink()
    except: pass

def ts(x):
    ms=int(x*1000)
    h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"
