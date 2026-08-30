const input = document.getElementById("video");
const selected = document.getElementById("selected");
const go = document.getElementById("go");
const bar = document.getElementById("bar");
const status = document.getElementById("status");
const result = document.getElementById("result");

const preview = document.getElementById("preview");
const video = document.getElementById("videoPreview");
const area = document.getElementById("subtitleArea");
const percent = document.getElementById("percent");

let file = null;
let jobId = null;

let dragging = false;
let resizing = false;

let startX = 0;
let startY = 0;

let region = {
    x: 0.05,
    y: 0.68,
    w: 0.90,
    h: 0.27
};


// =====================================================
// CHỌN VIDEO
// =====================================================

input.addEventListener("change", () => {

    if (!input.files || input.files.length === 0) {

        file = null;

        selected.textContent =
            "Chưa chọn video";

        go.disabled = true;

        if (preview) {
            preview.hidden = true;
        }

        return;
    }

    file = input.files[0];

    selected.textContent =
        "✓ " +
        file.name +
        " • " +
        formatSize(file.size);

    go.disabled = false;

    status.textContent =
        "Video đã sẵn sàng. Đang chuẩn bị trình chỉnh sửa…";

    if (preview) {
        preview.hidden = false;
    }

    const url =
        URL.createObjectURL(file);

    video.src = url;
    video.load();
});


// =====================================================
// DUNG LƯỢNG FILE
// =====================================================

function formatSize(bytes) {

    if (bytes < 1024 * 1024) {

        return (
            (bytes / 1024).toFixed(1) +
            " KB"
        );
    }

    return (
        (bytes / 1024 / 1024).toFixed(1) +
        " MB"
    );
}


// =====================================================
// VIDEO LOAD
// TỰ ĐỘNG GIỮ NGUYÊN TỈ LỆ VIDEO
// =====================================================

video.addEventListener(
    "loadedmetadata",
    () => {

        const width =
            video.videoWidth;

        const height =
            video.videoHeight;

        if (width && height) {

            const wrap =
                document.querySelector(
                    ".video-wrap"
                );

            if (wrap) {

                // 16:9, 9:16, 4:3, 1:1...
                // đều tự lấy đúng tỉ lệ gốc

                wrap.style.aspectRatio =
                    `${width} / ${height}`;
            }
        }

        resetRegion();

        status.textContent =
            "Kéo khung xanh đến đúng vị trí phụ đề cũ.";
    }
);


// =====================================================
// RESET VÙNG PHỤ ĐỀ
// =====================================================

function resetRegion() {

    region = {
        x: 0.05,
        y: 0.68,
        w: 0.90,
        h: 0.27
    };

    updateArea();
}


// =====================================================
// CẬP NHẬT KHUNG
// =====================================================

function updateArea() {

    if (!area || !video) {
        return;
    }

    area.style.left =
        (region.x * 100) + "%";

    area.style.top =
        (region.y * 100) + "%";

    area.style.width =
        (region.w * 100) + "%";

    area.style.height =
        (region.h * 100) + "%";
}


// =====================================================
// BẮT ĐẦU KÉO KHUNG
// =====================================================

if (area) {

    area.addEventListener(
        "pointerdown",
        startDrag
    );
}


function startDrag(e) {

    if (
        e.target.classList.contains(
            "resize"
        )
    ) {

        startResize(e);

        return;
    }

    dragging = true;

    area.setPointerCapture(
        e.pointerId
    );

    startX = e.clientX;
    startY = e.clientY;

    area.classList.add(
        "moving"
    );

    e.preventDefault();
}


// =====================================================
// DI CHUYỂN / RESIZE
// =====================================================

document.addEventListener(
    "pointermove",
    e => {

        if (!dragging && !resizing) {
            return;
        }

        const rect =
            video.getBoundingClientRect();

        if (!rect.width || !rect.height) {
            return;
        }

        const dx =
            (e.clientX - startX) /
            rect.width;

        const dy =
            (e.clientY - startY) /
            rect.height;


        // ---------------------------------------------
        // KÉO
        // ---------------------------------------------

        if (dragging) {

            region.x += dx;
            region.y += dy;

            region.x =
                Math.max(
                    0,
                    Math.min(
                        1 - region.w,
                        region.x
                    )
                );

            region.y =
                Math.max(
                    0,
                    Math.min(
                        1 - region.h,
                        region.y
                    )
                );
        }


        // ---------------------------------------------
        // RESIZE
        // ---------------------------------------------

        if (resizing) {

            region.w += dx;
            region.h += dy;

            region.w =
                Math.max(
                    0.05,
                    Math.min(
                        1 - region.x,
                        region.w
                    )
                );

            region.h =
                Math.max(
                    0.05,
                    Math.min(
                        1 - region.y,
                        region.h
                    )
                );
        }

        startX = e.clientX;
        startY = e.clientY;

        updateArea();

        e.preventDefault();
    },
    {
        passive: false
    }
);


// =====================================================
// THẢ CHUỘT / NGÓN TAY
// =====================================================

document.addEventListener(
    "pointerup",
    () => {

        dragging = false;
        resizing = false;

        if (area) {

            area.classList.remove(
                "moving"
            );
        }
    }
);


// =====================================================
// RESIZE HANDLE
// =====================================================

function startResize(e) {

    resizing = true;

    area.setPointerCapture(
        e.pointerId
    );

    startX = e.clientX;
    startY = e.clientY;

    e.stopPropagation();
    e.preventDefault();
}


// =====================================================
// BẮT ĐẦU DỊCH
// =====================================================

go.addEventListener(
    "click",
    async () => {

        if (!file) {

            status.textContent =
                "❌ Chưa chọn video.";

            return;
        }

        go.disabled = true;

        result.hidden = true;

        bar.value = 5;

        updatePercent(5);

        status.textContent =
            "Đang upload video…";


        try {

            // =============================================
            // UPLOAD
            // =============================================

            const form =
                new FormData();

            form.append(
                "video",
                file,
                file.name
            );

            const uploadResponse =
                await fetch(
                    "/api/upload",
                    {
                        method: "POST",
                        body: form
                    }
                );

            const uploadData =
                await uploadResponse.json();

            if (!uploadResponse.ok) {

                throw new Error(
                    uploadData.error ||
                    "Upload video thất bại."
                );
            }

            jobId =
                uploadData.job_id;

            bar.value = 10;

            updatePercent(10);

            status.textContent =
                "Upload thành công. Đang bắt đầu xử lý…";


            // =============================================
            // BẮT ĐẦU TRANSLATE
            // =============================================

            const translateResponse =
                await fetch(
                    "/api/translate/" +
                    jobId,
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body: JSON.stringify({
                            region: region
                        })
                    }
                );

            const translateData =
                await translateResponse.json();

            if (!translateResponse.ok) {

                throw new Error(
                    translateData.error ||
                    "Không thể bắt đầu dịch."
                );
            }


            // =============================================
            // THEO DÕI
            // =============================================

            await watchJob(jobId);

        } catch (error) {

            console.error(error);

            status.textContent =
                "❌ " +
                (
                    error.message ||
                    "Có lỗi xảy ra."
                );

            go.disabled = false;
        }
    }
);


// =====================================================
// THEO DÕI TIẾN ĐỘ
// =====================================================

async function watchJob(id) {

    while (true) {

        await sleep(1200);

        const response =
            await fetch(
                "/api/status/" +
                id +
                "?t=" +
                Date.now()
            );

        if (!response.ok) {

            throw new Error(
                "Không lấy được trạng thái xử lý."
            );
        }

        const data =
            await response.json();


        // =============================================
        // PROGRESS
        // =============================================

        if (
            typeof data.progress ===
            "number"
        ) {

            bar.value =
                data.progress;

            updatePercent(
                data.progress
            );
        }


        if (data.status_text) {

            status.textContent =
                data.status_text;
        }


        // =============================================
        // HOÀN TẤT
        // =============================================

        if (
            data.status ===
            "done"
        ) {

            bar.value = 100;

            updatePercent(100);

            status.textContent =
                "🎉 Hoàn tất! Video Vietsub đã sẵn sàng.";

            result.href =
                "/api/result/" +
                id;

            result.hidden = false;

            go.disabled = false;

            return;
        }


        // =============================================
        // LỖI
        // =============================================

        if (
            data.status ===
            "error"
        ) {

            throw new Error(
                data.error ||
                "Server không thể xử lý video."
            );
        }
    }
}


// =====================================================
// HIỂN THỊ %
// =====================================================

function updatePercent(value) {

    if (!percent) {
        return;
    }

    percent.textContent =
        Math.round(value) +
        "%";
}


// =====================================================
// DELAY
// =====================================================

function sleep(ms) {

    return new Promise(
        resolve =>
            setTimeout(
                resolve,
                ms
            )
    );
}


// =====================================================
// PHÍM R = RESET
// =====================================================

document.addEventListener(
    "keydown",
    e => {

        if (
            e.key.toLowerCase() ===
            "r"
        ) {

            if (
                document.activeElement &&
                document.activeElement.tagName ===
                "INPUT"
            ) {
                return;
            }

            resetRegion();

            status.textContent =
                "Đã đặt lại vùng phụ đề.";
        }
    }
);
