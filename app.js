const $ = (id) => document.getElementById(id);

const fileInput = $("video");
const selected = $("selected");
const dropzone = $("dropzone");
const preview = $("preview");
const videoPreview = $("videoPreview");
const subtitleArea = $("subtitleArea");
const go = $("go");
const bar = $("bar");
const percent = $("percent");
const statusEl = $("status");
const result = $("result");

let file = null;
let jobId = null;
let pollTimer = null;
let uploadXhr = null;
let region = { x: 0.05, y: 0.68, w: 0.90, h: 0.27 };

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function setProgress(value, text) {
    value = clamp(Math.round(value), 0, 100);
    bar.value = value;
    percent.textContent = `${value}%`;
    if (text) statusEl.textContent = text;
}

function resetUI() {
    setProgress(0, "Chọn một video để bắt đầu.");
    go.disabled = !file;
    result.hidden = true;
    result.removeAttribute("href");
}

function updateRegionBox() {
    subtitleArea.style.left = `${region.x * 100}%`;
    subtitleArea.style.top = `${region.y * 100}%`;
    subtitleArea.style.width = `${region.w * 100}%`;
    subtitleArea.style.height = `${region.h * 100}%`;
}

function resetRegion() {
    region = { x: 0.05, y: 0.68, w: 0.90, h: 0.27 };
    updateRegionBox();
}

function chooseFile(f) {
    if (!f) return;
    if (!f.type.startsWith("video/") && !/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(f.name)) {
        statusEl.textContent = "File không phải video.";
        return;
    }

    file = f;
    selected.textContent = `${f.name} · ${formatBytes(f.size)}`;
    go.disabled = false;
    result.hidden = true;
    jobId = null;
    if (pollTimer) clearTimeout(pollTimer);

    const url = URL.createObjectURL(f);
    videoPreview.src = url;
    videoPreview.load();
    preview.hidden = false;
    requestAnimationFrame(updateRegionBox);
    statusEl.textContent = "Video đã chọn. Có thể bắt đầu dịch.";
    setProgress(0, statusEl.textContent);
}

function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));

["dragenter", "dragover"].forEach(type => {
    dropzone.addEventListener(type, e => {
        e.preventDefault();
        dropzone.classList.add("drag");
    });
});
["dragleave", "drop"].forEach(type => {
    dropzone.addEventListener(type, e => {
        e.preventDefault();
        dropzone.classList.remove("drag");
    });
});
dropzone.addEventListener("drop", e => chooseFile(e.dataTransfer.files[0]));

function uploadFile() {
    return new Promise((resolve, reject) => {
        uploadXhr = new XMLHttpRequest();
        uploadXhr.open("POST", "/api/upload", true);
        uploadXhr.responseType = "json";
        uploadXhr.timeout = 0;

        uploadXhr.upload.addEventListener("progress", e => {
            if (e.lengthComputable) {
                // Keep 0-55% for upload, then 55-100% for processing.
                const p = (e.loaded / e.total) * 55;
                setProgress(p, `Đang tải video lên… ${Math.round(e.loaded / e.total * 100)}%`);
            }
        });

        uploadXhr.onload = () => {
            let data = uploadXhr.response;
            if (!data && uploadXhr.responseText) {
                try { data = JSON.parse(uploadXhr.responseText); } catch (_) {}
            }
            if (uploadXhr.status >= 200 && uploadXhr.status < 300 && data?.ok) {
                setProgress(55, "Đã tải video lên. Đang chuẩn bị dịch…");
                resolve(data.job_id);
            } else {
                reject(new Error(data?.error || `Upload thất bại (${uploadXhr.status}).`));
            }
        };
        uploadXhr.onerror = () => reject(new Error("Mất kết nối khi tải video lên."));
        uploadXhr.onabort = () => reject(new Error("Upload đã bị hủy."));
        uploadXhr.ontimeout = () => reject(new Error("Upload mất quá nhiều thời gian."));

        const fd = new FormData();
        fd.append("video", file, file.name);
        uploadXhr.send(fd);
    });
}

async function startTranslation(id) {
    const response = await fetch(`/api/translate/${encodeURIComponent(id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ region })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || "Không thể bắt đầu dịch.");
}

async function pollStatus(id) {
    try {
        const response = await fetch(`/api/status/${encodeURIComponent(id)}`, {
            cache: "no-store"
        });
        const data = await response.json();

        if (!response.ok) throw new Error(data.error || "Không đọc được trạng thái.");

        setProgress(data.progress ?? 0, data.status_text || "Đang xử lý…");

        if (data.status === "done") {
            result.href = `/api/result/${encodeURIComponent(id)}`;
            result.hidden = false;
            go.disabled = false;
            return;
        }

        if (data.status === "error") {
            throw new Error(data.error || "Xử lý video thất bại.");
        }

        pollTimer = setTimeout(() => pollStatus(id), 900);
    } catch (err) {
        statusEl.textContent = `❌ ${err.message}`;
        go.disabled = false;
    }
}

go.addEventListener("click", async () => {
    if (!file || go.disabled) return;

    go.disabled = true;
    result.hidden = true;
    setProgress(0, "Đang bắt đầu upload…");

    try {
        jobId = await uploadFile();
        await startTranslation(jobId);
        setProgress(56, "Đang xử lý: OCR → dịch → lồng tiếng → render…");
        await pollStatus(jobId);
    } catch (err) {
        statusEl.textContent = `❌ ${err.message}`;
        go.disabled = false;
    }
});

// Simple pointer-based move/resize editor.
// Drag inside = move. Drag the bottom-right handle = resize.
let pointer = null;

subtitleArea.addEventListener("pointerdown", e => {
    if (e.target === subtitleArea || e.target.classList.contains("area-label")) {
        pointer = {
            mode: "move",
            sx: e.clientX,
            sy: e.clientY,
            start: { ...region }
        };
    } else if (e.target.classList.contains("resize")) {
        pointer = {
            mode: "resize",
            sx: e.clientX,
            sy: e.clientY,
            start: { ...region }
        };
    }
    if (pointer) {
        subtitleArea.setPointerCapture(e.pointerId);
        e.preventDefault();
    }
});

subtitleArea.addEventListener("pointermove", e => {
    if (!pointer) return;
    const rect = videoPreview.getBoundingClientRect();
    const dx = (e.clientX - pointer.sx) / rect.width;
    const dy = (e.clientY - pointer.sy) / rect.height;

    if (pointer.mode === "move") {
        region.x = clamp(pointer.start.x + dx, 0, 1 - region.w);
        region.y = clamp(pointer.start.y + dy, 0, 1 - region.h);
    } else {
        region.w = clamp(pointer.start.w + dx, 0.02, 1 - region.x);
        region.h = clamp(pointer.start.h + dy, 0.02, 1 - region.y);
    }
    updateRegionBox();
});

subtitleArea.addEventListener("pointerup", () => { pointer = null; });
subtitleArea.addEventListener("pointercancel", () => { pointer = null; });

window.addEventListener("resize", updateRegionBox);
window.addEventListener("keydown", e => {
    if (e.key.toLowerCase() === "r" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
        resetRegion();
    }
});

resetRegion();
resetUI();
