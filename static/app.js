(function () {
  "use strict";

  var $ = document.getElementById.bind(document);

  var dropZone = $("drop-zone");
  var fileInput = $("file-input");
  var progressView = $("progress-view");
  var statusLabel = $("status-label");
  var progressBar = $("progress-bar");
  var progressDetail = $("progress-detail");
  var doneView = $("done-view");
  var resultInfo = $("result-info");
  var downloadLink = $("download-link");
  var errorView = $("error-view");
  var errorMsg = $("error-msg");

  var views = [dropZone, progressView, doneView, errorView];
  var lastFile = null;
  var activeXhr = null;
  var pollTimer = null;

  function show(el) {
    views.forEach(function (v) { v.classList.add("hidden"); });
    el.classList.remove("hidden");
  }

  function fmtBytes(b) {
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }

  function abort() {
    if (activeXhr) { activeXhr.abort(); activeXhr = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* ---- drag & drop ---- */

  dropZone.addEventListener("click", function () { fileInput.click(); });

  dropZone.addEventListener("dragover", function (e) {
    e.preventDefault();
    dropZone.classList.add("over");
  });

  dropZone.addEventListener("dragleave", function () {
    dropZone.classList.remove("over");
  });

  dropZone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropZone.classList.remove("over");
    if (e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);
  });

  fileInput.addEventListener("change", function () {
    if (fileInput.files[0]) pick(fileInput.files[0]);
  });

  /* ---- file validation ---- */

  var VIDEO_EXT = /\.(mp4|mov|avi|mkv|webm|mpeg|mpg|wmv|flv|3gp|m4v|ts|mts|ogv)$/i;

  function pick(file) {
    if (!file.type.startsWith("video/") && !VIDEO_EXT.test(file.name)) {
      return showError("Please select a video file.");
    }
    if (file.size > 500 * 1024 * 1024) {
      return showError("File exceeds the 500 MB limit.");
    }
    lastFile = file;
    upload(file);
  }

  /* ---- upload ---- */

  function upload(file) {
    abort();
    show(progressView);
    statusLabel.textContent = "Uploading\u2026";
    progressBar.className = "bar";
    progressBar.style.width = "0%";
    progressDetail.textContent = file.name + " \u00B7 " + fmtBytes(file.size);

    var fd = new FormData();
    fd.append("file", file);
    var xhr = new XMLHttpRequest();
    activeXhr = xhr;

    xhr.upload.addEventListener("progress", function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round((e.loaded / e.total) * 100);
      progressBar.style.width = pct + "%";
      progressDetail.textContent = pct + "% of " + fmtBytes(e.total);
    });

    xhr.addEventListener("load", function () {
      activeXhr = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          startPolling(JSON.parse(xhr.responseText).job_id);
        } catch (_) {
          showError("Unexpected server response.");
        }
      } else {
        try {
          showError(JSON.parse(xhr.responseText).detail);
        } catch (_) {
          showError("Upload failed (HTTP " + xhr.status + ").");
        }
      }
    });

    xhr.addEventListener("error", function () {
      activeXhr = null;
      showError("Connection lost. Check your network and try again.");
    });

    xhr.addEventListener("abort", function () { activeXhr = null; });

    xhr.open("POST", "/api/upload");
    xhr.send(fd);
  }

  /* ---- poll for job status ---- */

  var seenProcessing = false;

  function startPolling(jobId) {
    seenProcessing = false;
    statusLabel.textContent = "Queued\u2026";
    progressBar.className = "bar wait";
    progressBar.style.width = "";
    progressDetail.textContent = "Waiting for available slot\u2026";

    var failures = 0;

    pollTimer = setInterval(function () {
      fetch("/api/status/" + jobId)
        .then(function (r) {
          if (!r.ok && r.status === 404) {
            clearInterval(pollTimer); pollTimer = null;
            showError("Job expired. Please upload again.");
            return null;
          }
          return r.json();
        })
        .then(function (d) {
          if (!d) return;
          failures = 0;

          if (d.status === "queued") {
            statusLabel.textContent = "Queued\u2026";
            progressBar.className = "bar wait";
            var pos = d.position || 0;
            progressDetail.textContent = pos > 0
              ? pos + " job" + (pos !== 1 ? "s" : "") + " ahead"
              : "Starting soon\u2026";

          } else if (d.status === "processing") {
            if (!seenProcessing) {
              seenProcessing = true;
              statusLabel.textContent = "Converting video\u2026";
              progressBar.className = "bar pulse";
              progressBar.style.width = "";
            }
            if (d.progress > 0) {
              progressBar.className = "bar";
              progressBar.style.width = d.progress + "%";
              statusLabel.textContent = Math.round(d.progress) + "% converted";
            }
            var info = [];
            if (d.video_codec) info.push(d.video_codec + " \u2192 h264");
            if (d.resolution) info.push(d.resolution);
            progressDetail.textContent = info.length
              ? info.join(" \u00B7 ")
              : "Converting to universal format\u2026";

          } else if (d.status === "done") {
            clearInterval(pollTimer); pollTimer = null;
            statusLabel.textContent = "Preparing download\u2026";
            progressDetail.textContent = "";
            setTimeout(function () { showDone(jobId, d); }, 500);

          } else if (d.status === "error") {
            clearInterval(pollTimer); pollTimer = null;
            showError(d.error || "Conversion failed.");
          }
        })
        .catch(function () {
          failures++;
          if (failures > 20) {
            clearInterval(pollTimer); pollTimer = null;
            showError("Lost connection to server.");
          }
        });
    }, 1500);
  }

  /* ---- result states ---- */

  function showDone(jobId, data) {
    show(doneView);
    downloadLink.href = "/api/download/" + jobId;

    var parts = [];
    if (data.video_codec) parts.push(data.video_codec + " \u2192 h264");
    if (data.output_size) parts.push(fmtBytes(data.output_size));
    if (data.conversion_time) parts.push(data.conversion_time + "s");
    resultInfo.textContent = parts.join("  \u00B7  ");
  }

  function showError(msg) {
    abort();
    show(errorView);
    errorMsg.textContent = msg || "Something went wrong.";
  }

  function reset() {
    abort();
    lastFile = null;
    fileInput.value = "";
    show(dropZone);
  }

  /* ---- buttons ---- */

  $("btn-another").addEventListener("click", reset);

  $("btn-retry").addEventListener("click", function () {
    if (lastFile) {
      upload(lastFile);
    } else {
      reset();
    }
  });
})();
