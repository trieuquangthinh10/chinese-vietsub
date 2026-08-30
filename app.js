const input = document.getElementById("video");
const selected = document.getElementById("selected");
const go = document.getElementById("go");
const bar = document.getElementById("bar");
const status = document.getElementById("status");
const result = document.getElementById("result");

let file = null;

input.addEventListener("change", function () {
  if (this.files && this.files.length > 0) {
    file = this.files[0];

    selected.textContent =
      "✅ " + file.name + " • " +
      (file.size / 1024 / 1024).toFixed(1) + " MB";

    status.textContent = "Đã chọn video. Bấm Bắt đầu dịch.";
    go.disabled = false;
  }
});

go.addEventListener("click", async function () {
  if (!file) {
    status.textContent = "❌ Chưa chọn video";
    return;
  }

  go.disabled = true;
  result.hidden = true;
  bar.value = 5;

  try {
    status.textContent = "Đang upload video…";

    const fd = new FormData();
    fd.append("video", file);

    const up = await fetch("/api/upload", {
      method: "POST",
      body: fd
    });

    const uj = await up.json();

    if (!up.ok) {
      throw new Error(uj.error || "Upload thất bại");
    }

    bar.value = 15;
    status.textContent = "Đang bắt đầu dịch…";

    const tr = await fetch("/api/translate/" + uj.job_id, {
      method: "POST"
    });

    const tj = await tr.json();

    if (!tr.ok) {
      throw new Error(tj.error || "Không thể bắt đầu dịch");
    }

    const timer = setInterval(async () => {
      try {
        const r = await fetch("/api/status/" + uj.job_id);
        const s = await r.json();

        bar.value = s.progress || 0;
        status.textContent =
          s.status_text || s.status || "Đang xử lý…";

        if (s.status === "done") {
          clearInterval(timer);

          bar.value = 100;
          status.textContent = "🎉 Hoàn tất!";

          result.href = "/api/result/" + uj.job_id;
          result.hidden = false;
          go.disabled = false;
        }

        if (s.status === "error") {
          clearInterval(timer);

          status.textContent =
            "❌ " + (s.error || "Có lỗi xảy ra");

          go.disabled = false;
        }

      } catch (e) {
        clearInterval(timer);
        status.textContent = "❌ " + e.message;
        go.disabled = false;
      }
    }, 1000);

  } catch (e) {
    status.textContent = "❌ " + e.message;
    go.disabled = false;
  }
});
