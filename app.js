const input = document.getElementById("video");
const dropzone = document.getElementById("dropzone");
const fileInfo = document.getElementById("fileInfo");
const fileName = document.getElementById("fileName");
const fileSize = document.getElementById("fileSize");
const removeFile = document.getElementById("removeFile");
const previewCard = document.getElementById("previewCard");
const preview = document.getElementById("preview");
const go = document.getElementById("go");
const status = document.getElementById("status");
const bar = document.getElementById("bar");
const percent = document.getElementById("percent");
const result = document.getElementById("result");
const themeBtn = document.getElementById("themeBtn");

let file = null;
let objectUrl = null;
let timer = null;

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function setProgress(value, text) {
  const p = Math.max(0, Math.min(100, Number(value) || 0));
  bar.style.width = p + "%";
  percent.textContent = p + "%";
  if (text) status.textContent = text;
}

function chooseFile(selectedFile) {
  if (!selectedFile) return;

  file = selectedFile;
  fileName.textContent = file.name;
  fileSize.textContent = formatSize(file.size);

  fileInfo.classList.remove("hidden");
  dropzone.classList.add("has-file");
  previewCard.classList.remove("hidden");
  go.disabled = false;
  result.classList.add("hidden");
  setProgress(0, "Đã chọn video. Bấm “Bắt đầu dịch”.");

  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(file);
  preview.src = objectUrl;
  preview.load();
}

/* iPhone/Safari: change là sự kiện chính; input chỉ reset sau khi xoá. */
input.addEventListener("change", () => {
  chooseFile(input.files && input.files[0]);
});

removeFile.addEventListener("click", (e) => {
  e.preventDefault();
  file = null;
  input.value = "";
  fileInfo.classList.add("hidden");
  dropzone.classList.remove("has-file");
  previewCard.classList.add("hidden");
  preview.removeAttribute("src");
  go.disabled = true;
  setProgress(0, "Chọn một video để bắt đầu.");
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
});

["dragenter", "dragover"].forEach(type => {
  dropzone.addEventListener(type, e => {
    e.preventDefault();
    dropzone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach(type => {
  dropzone.addEventListener(type, e => {
    e.preventDefault();
    dropzone.classList.remove("dragging");
  });
});
dropzone.addEventListener("drop", e => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  chooseFile(f);
});

go.addEventListener("click", async () => {
  if (!file) return;

  go.disabled = true;
  result.classList.add("hidden");
  setProgress(5, "Đang upload video…");

  try {
    const fd = new FormData();
    fd.append("video", file, file.name);

    const up = await fetch("/api/upload", { method: "POST", body: fd });
    const uj = await up.json();
    if (!up.ok) throw new Error(uj.error || "Upload thất bại.");

    setProgress(15, "Đang bắt đầu dịch…");

    const tr = await fetch("/api/translate/" + encodeURIComponent(uj.job_id), {
      method: "POST"
    });
    if (!tr.ok) {
      const tj = await tr.json().catch(() => ({}));
      throw new Error(tj.error || "Không thể bắt đầu dịch.");
    }

    clearInterval(timer);
    timer = setInterval(async () => {
      try {
        const r = await fetch("/api/status/" + encodeURIComponent(uj.job_id), {
          cache: "no-store"
        });
        const s = await r.json();

        setProgress(s.progress || 0, s.status_text || "Đang xử lý…");

        if (s.status === "done") {
          clearInterval(timer);
          setProgress(100, "🎉 Vietsub đã hoàn tất!");
          result.href = "/api/result/" + encodeURIComponent(uj.job_id);
          result.classList.remove("hidden");
          go.disabled = false;
        }

        if (s.status === "error") {
          clearInterval(timer);
          status.textContent = "❌ " + (s.error || "Có lỗi xảy ra.");
          go.disabled = false;
        }
      } catch (err) {
        clearInterval(timer);
        status.textContent = "❌ " + err.message;
        go.disabled = false;
      }
    }, 1000);
  } catch (err) {
    status.textContent = "❌ " + err.message;
    go.disabled = false;
  }
});

themeBtn.addEventListener("click", () => {
  document.body.classList.toggle("light");
  themeBtn.textContent = document.body.classList.contains("light") ? "☾" : "☼";
});
