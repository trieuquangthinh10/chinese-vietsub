coónt const input = document.getElementById("video");
const go = document.getElementById("go");
const status = document.getElementById("status");
const bar = document.getElementById("bar");
const percent = document.getElementById("percent");
const result = document.getElementById("result");

let file = null;

input.addEventListener("change", function () {
    file = this.files && this.files.length ? this.files[0] : null;

    if (!file) {
        go.disabled = true;
        status.textContent = "Chưa chọn video";
        return;
    }

    go.disabled = false;
    status.textContent = "Đã chọn: " + file.name;
});

function progress(p, text) {
    p = Math.max(0, Math.min(100, p));
    bar.style.width = p + "%";
    percent.textContent = Math.round(p) + "%";
    if (text) status.textContent = text;
}

go.addEventListener("click", function () {
    if (!file) {
        status.textContent = "❌ Chưa chọn video";
        return;
    }

    go.disabled = true;
    result.classList.add("hidden");

    const fd = new FormData();
    fd.append("video", file, file.name);

    progress(0, "Đang chuẩn bị upload…");

    const xhr = new XMLHttpRequest();

    xhr.open("POST", "/api/upload");

    xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) {
            const p = (e.loaded / e.total) * 15;
            progress(p, "Đang upload video… " + Math.round((e.loaded / e.total) * 100) + "%");
        }
    };

    xhr.onload = async function () {
        if (xhr.status < 200 || xhr.status >= 300) {
            let msg = "Upload thất bại";
            try {
                msg = JSON.parse(xhr.responseText).error || msg;
            } catch {}
            progress(0, "❌ " + msg);
            go.disabled = false;
            return;
        }

        const data = JSON.parse(xhr.responseText);
        const jid = data.job_id;

        progress(15, "Đã upload. Đang bắt đầu dịch…");

        const tr = await fetch("/api/translate/" + encodeURIComponent(jid), {
            method: "POST"
        });

        if (!tr.ok) {
            progress(0, "❌ Không thể bắt đầu dịch");
            go.disabled = false;
            return;
        }

        const timer = setInterval(async () => {
            try {
                const r = await fetch("/api/status/" + encodeURIComponent(jid));
                const s = await r.json();

                progress(
                    s.progress || 0,
                    s.status_text || "Đang xử lý…"
                );

                if (s.status === "done") {
                    clearInterval(timer);
                    progress(100, "🎉 Hoàn tất!");
                    result.href = "/api/result/" + encodeURIComponent(jid);
                    result.classList.remove("hidden");
                    go.disabled = false;
                }

                if (s.status === "error") {
                    clearInterval(timer);
                    progress(0, "❌ " + (s.error || "Có lỗi xảy ra"));
                    go.disabled = false;
                }

            } catch (e) {
                clearInterval(timer);
                progress(0, "❌ Mất kết nối server");
                go.disabled = false;
            }
        }, 1000);
    };

    xhr.onerror = function () {
        progress(0, "❌ Upload lỗi hoặc mất kết nối");
        go.disabled = false;
    };

    xhr.send(fd);
});
