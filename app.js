const input = document.getElementById("video");
const selected = document.getElementById("selected");
const go = document.getElementById("go");
const bar = document.getElementById("bar");
const status = document.getElementById("status");
const result = document.getElementById("result");
let file = null;

function chooseFile() {
  file = input.files && input.files.length ? input.files[0] : null;
  if (!file) {
    selected.textContent = "Chưa chọn video";
    go.disabled = true;
    return;
  }
  selected.textContent = "✅ " + file.name + " • " + (file.size / 1024 / 1024).toFixed(1) + " MB";
  status.textContent = "Đã chọn video. Bấm Bắt đầu dịch.";
  go.disabled = false;
}
input.addEventListener("change", chooseFile);
input.addEventListener("input", chooseFile);

go.addEventListener("click", async () => {
  if (!file) return;
  go.disabled = true;
  result.hidden = true;
  bar.value = 5;
  status.textContent = "Đang upload video…";
  try {
    const fd = new FormData();
    fd.append("video", file, file.name);
    const up = await fetch("/api/upload", {method:"POST", body:fd});
    const uj = await up.json();
    if (!up.ok) throw new Error(uj.error || "Upload failed");

    bar.value = 15;
    status.textContent = "Đang bắt đầu dịch…";
    const tr = await fetch("/api/translate/" + uj.job_id, {method:"POST"});
    if (!tr.ok) throw new Error("Không thể bắt đầu dịch");

    const timer = setInterval(async () => {
      try {
        const r = await fetch("/api/status/" + uj.job_id);
        const s = await r.json();
        bar.value = s.progress || 0;
        status.textContent = s.status_text || s.status || "Đang xử lý…";
        if (s.status === "done") {
          clearInterval(timer);
          bar.value = 100;
          status.textContent = "🎉 Hoàn tất!";
          result.href = "/api/result/" + uj.job_id;
          result.hidden = false;
          go.disabled = false;
        } else if (s.status === "error") {
          clearInterval(timer);
          status.textContent = "❌ " + (s.error || "Có lỗi xảy ra");
          go.disabled = false;
        }
      } catch(e) {
        clearInterval(timer);
        status.textContent = "❌ " + e.message;
        go.disabled = false;
      }
    }, 1000);
  } catch(e) {
    status.textContent = "❌ " + e.message;
    go.disabled = false;
  }
});