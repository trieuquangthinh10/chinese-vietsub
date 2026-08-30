const input = document.getElementById("video");
const dropzone = document.getElementById("dropzone");
const previewWrap = document.getElementById("previewWrap");
const preview = document.getElementById("preview");
const selector = document.getElementById("selector");
const regionInfo = document.getElementById("regionInfo");
const hint = document.getElementById("hint");
const go = document.getElementById("go");
const bar = document.getElementById("bar");
const status = document.getElementById("status");
const percent = document.getElementById("percent");
const result = document.getElementById("result");
const fileMeta = document.getElementById("fileMeta");

let file = null;
let region = {x: .12, y: .70, w: .76, h: .18};
let drag = null;

function clamp(v, a, b){ return Math.max(a, Math.min(b, v)); }

function setRegion(){
  selector.style.left = (region.x * 100) + "%";
  selector.style.top = (region.y * 100) + "%";
  selector.style.width = (region.w * 100) + "%";
  selector.style.height = (region.h * 100) + "%";
  document.getElementById("rx").textContent = Math.round(region.x*100)+"%";
  document.getElementById("ry").textContent = Math.round(region.y*100)+"%";
  document.getElementById("rw").textContent = Math.round(region.w*100)+"%";
  document.getElementById("rh").textContent = Math.round(region.h*100)+"%";
}

function chooseFile(){
  file = input.files && input.files.length ? input.files[0] : null;
  if(!file) return;

  preview.src = URL.createObjectURL(file);
  previewWrap.hidden = false;
  regionInfo.hidden = false;
  hint.hidden = false;
  fileMeta.textContent = (file.size/1024/1024).toFixed(1) + " MB";
  status.textContent = "Kéo khung để chọn vùng phụ đề.";
  go.disabled = false;
  setRegion();
}

input.addEventListener("change", chooseFile);

selector.addEventListener("pointerdown", e => {
  e.preventDefault();
  selector.setPointerCapture(e.pointerId);
  const r = previewWrap.getBoundingClientRect();
  drag = {
    sx: e.clientX, sy: e.clientY,
    ox: region.x, oy: region.y
  };
});

selector.addEventListener("pointermove", e => {
  if(!drag) return;
  const r = previewWrap.getBoundingClientRect();
  region.x = clamp(drag.ox + (e.clientX-drag.sx)/r.width, 0, 1-region.w);
  region.y = clamp(drag.oy + (e.clientY-drag.sy)/r.height, 0, 1-region.h);
  setRegion();
});

selector.addEventListener("pointerup", () => drag = null);
selector.addEventListener("pointercancel", () => drag = null);

go.addEventListener("click", async () => {
  if(!file) return;

  go.disabled = true;
  result.hidden = true;
  bar.style.width = "5%";
  percent.textContent = "5%";
  status.textContent = "Đang upload video…";

  try{
    const fd = new FormData();
    fd.append("video", file, file.name);

    const up = await fetch("/api/upload", {method:"POST", body:fd});
    const uj = await up.json();
    if(!up.ok) throw new Error(uj.error || "Upload thất bại");

    status.textContent = "Đang khởi động AI…";
    const tr = await fetch("/api/translate/"+uj.job_id, {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({region})
    });
    const tj = await tr.json();
    if(!tr.ok) throw new Error(tj.error || "Không thể bắt đầu dịch");

    const timer = setInterval(async () => {
      try{
        const r = await fetch("/api/status/"+uj.job_id);
        const s = await r.json();
        const p = Number(s.progress || 0);
        bar.style.width = p + "%";
        percent.textContent = p + "%";
        status.textContent = s.status_text || s.status || "Đang xử lý…";

        if(s.status === "done"){
          clearInterval(timer);
          bar.style.width = "100%";
          percent.textContent = "100%";
          status.textContent = "Vietsub đã sẵn sàng.";
          result.href = "/api/result/"+uj.job_id;
          result.hidden = false;
          go.disabled = false;
        }else if(s.status === "error"){
          clearInterval(timer);
          throw new Error(s.error || "Có lỗi xảy ra");
        }
      }catch(e){
        clearInterval(timer);
        status.textContent = "❌ " + e.message;
        go.disabled = false;
      }
    },1000);

  }catch(e){
    status.textContent = "❌ " + e.message;
    go.disabled = false;
  }
});
