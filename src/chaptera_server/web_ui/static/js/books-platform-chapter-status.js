/* Adapted from user's books_platform snapshot: backend/app/web/static/js/chapter-import-status.js. */
(function () {
  "use strict";
  function text(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value);
  }
  function intValue(value) {
    const parsed = Number.parseInt(value, 10);
    if (Number.isNaN(parsed) || parsed < 0) {
      return 0;
    }
    return parsed;
  }
  function setText(root, selector, value) {
    const element = root.querySelector(selector);
    if (!element) {
      return;
    }
    element.textContent = text(value);
  }
  function setHidden(element, hidden) {
    if (!element) {
      return;
    }
    element.hidden = Boolean(hidden);
  }
  function statusBadgeClass(status) {
    const tones = {
      queued: "warning",
      running: "info",
      succeeded: "success",
      failed: "danger",
      cancelled: "neutral"
    };
    const tone = tones[status] || "neutral";
    return "ui-badge ui-badge--" + tone + " chapter-import-status";
  }
  function statusNote(status) {
    if (status === "queued") {
      return "Обработка начнётся автоматически.";
    }
    if (status === "running") {
      return "Можно оставить страницу открытой.";
    }
    if (status === "succeeded") {
      return "Готово. Проверьте результат в книге.";
    }
    if (status === "failed") {
      return "Проверьте файл и попробуйте снова.";
    }
    if (status === "cancelled") {
      return "Обработка отменена.";
    }
    return "Статус импорта обновлён.";
  }
  function renderPreview(root, preview) {
    const section = root.querySelector("[data-import-preview]");
    const list = root.querySelector("[data-import-preview-list]");
    const title = root.querySelector("[data-import-preview-title]");
    const more = root.querySelector("[data-import-preview-more]");
    const chapters = Array.isArray(preview.chapters) ? preview.chapters : [];
    if (!section || !list) {
      return;
    }
    if (!chapters.length) {
      section.hidden = true;
      list.innerHTML = "";
      return;
    }
    section.hidden = false;
    if (title) {
      const count = intValue(preview.chapter_count || preview.chapters_total || chapters.length);
      title.textContent = "Система распознала " + count + " глав.";
    }
    list.innerHTML = "";
    chapters.slice(0, 10).forEach(function (chapter) {
      const item = document.createElement("article");
      item.className = "chapter-import-preview__item";
      const meta = document.createElement("div");
      meta.className = "chapter-import-preview__meta";
      meta.textContent = "Глава " + text(chapter.number || chapter.chapter_no || "");
      const heading = document.createElement("h3");
      heading.className = "chapter-import-preview__chapter-title";
      heading.textContent = text(chapter.title || "Без названия");
      item.appendChild(meta);
      item.appendChild(heading);
      if (chapter.body_preview) {
        const body = document.createElement("p");
        body.className = "chapter-import-preview__body";
        body.textContent = text(chapter.body_preview);
        item.appendChild(body);
      }
      if (chapter.body_length) {
        const length = document.createElement("div");
        length.className = "chapter-import-preview__length";
        length.textContent = text(chapter.body_length) + " символов";
        item.appendChild(length);
      }
      list.appendChild(item);
    });
    if (more) {
      const total = intValue(preview.chapters_total || preview.chapter_count || chapters.length);
      more.hidden = total <= 10;
      more.textContent = "Показаны первые 10 глав из " + total + ".";
    }
  }
  function renderTrace(root, preview) {
    const section = root.querySelector("[data-import-trace]");
    const list = root.querySelector("[data-import-trace-list]");
    const trace = Array.isArray(preview.trace) ? preview.trace : [];
    if (!section || !list) {
      return;
    }
    if (!trace.length) {
      section.hidden = true;
      list.innerHTML = "";
      return;
    }
    section.hidden = false;
    list.innerHTML = "";
    trace.forEach(function (step) {
      const status = text(step.status || "unknown");
      const row = document.createElement("div");
      row.className = "chapter-import-trace-step chapter-import-trace-step--" + status;
      const statusNode = document.createElement("span");
      statusNode.className = "chapter-import-trace-step__status";
      statusNode.textContent = status === "ok" ? "✓" : status === "failed" ? "!" : "·";
      const toolNode = document.createElement("span");
      toolNode.className = "chapter-import-trace-step__tool";
      toolNode.textContent = text(step.tool || "");
      const stateNode = document.createElement("span");
      stateNode.className = "chapter-import-trace-step__state";
      stateNode.textContent = status;
      row.appendChild(statusNode);
      row.appendChild(toolNode);
      row.appendChild(stateNode);
      list.appendChild(row);
    });
  }
  function renderError(root, data) {
    const block = root.querySelector("[data-import-error]");
    const message = root.querySelector("[data-import-error-message]");
    const errorMessage = text(
      (data.error && data.error.message) ||
      data.error_message ||
      (data.preview && data.preview.error) ||
      ""
    );
    if (!block || !message) {
      return;
    }
    if (!errorMessage) {
      block.hidden = true;
      message.textContent = "";
      return;
    }
    block.hidden = false;
    message.textContent = errorMessage;
  }
  function renderStatus(root, data) {
    const status = text(data.status || "queued");
    const progress = data.progress || {};
    const result = data.result || {};
    const total = intValue(progress.total || data.total_count || 0);
    const current = intValue(progress.current || 0);
    const created = intValue(result.created_count || data.created_count || 0);
    const updated = intValue(result.updated_count || data.updated_count || 0);
    const skipped = intValue(result.skipped_count || data.skipped_count || 0);
    const failed = intValue(result.failed_count || data.failed_count || 0);
    setText(root, "[data-import-hero-text]", statusNote(status));
    setText(root, "[data-import-status-label]", data.status_label || status);
    setText(root, "[data-import-progress-label]", current + " из " + total);
    setText(root, "[data-import-total]", total);
    setText(root, "[data-import-created]", created);
    setText(root, "[data-import-updated]", updated);
    setText(root, "[data-import-skipped]", skipped);
    setText(root, "[data-import-failed]", failed);
    setText(root, "[data-import-footer-note]", statusNote(status));
    const badge = root.querySelector("[data-import-status-label]");
    if (badge) {
      badge.className = statusBadgeClass(status);
    }
    const progressBar = root.querySelector("[data-import-progress-bar]");
    if (progressBar) {
      progressBar.max = total > 0 ? total : 1;
      progressBar.value = current;
      progressBar.textContent = current + " из " + total;
    }
    const queuePosition = root.querySelector("[data-import-queue-position]");
    setHidden(queuePosition, !(status === "queued" && data.queue_position));
    if (queuePosition && data.queue_position) {
      setText(queuePosition, "[data-import-queue-position-value]", data.queue_position);
    }
    renderPreview(root, data.preview || {});
    renderTrace(root, data.preview || {});
    renderError(root, data);
    const footerNote = root.querySelector("[data-import-footer-note]");
    if (footerNote) {
      footerNote.className = "chapter-import-note";
    }
  }
  function init(root) {
    const statusUrl = root.getAttribute("data-import-status-url");
    if (!statusUrl) {
      return;
    }
    let stopped = false;
    async function poll() {
      if (stopped) {
        return;
      }
      try {
        const response = await fetch(statusUrl, {
          method: "GET",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json"
          }
        });
        if (!response.ok) {
          window.setTimeout(poll, 5000);
          return;
        }
        const data = await response.json();
        renderStatus(root, data);
        if (data.is_terminal) {
          stopped = true;
          return;
        }
      } catch (_error) {
        window.setTimeout(poll, 5000);
        return;
      }
      window.setTimeout(poll, 2500);
    }
    poll();
  }
  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll("[data-import-status-root]")
      .forEach(init);
  });
  // ReadEra binding: use the original safe DOM renderer with existing API data.
  // Polling is not started on the anonymous shell; /v1 requires X-API-Key.
  window.ReadEraStatus = Object.freeze({ renderStatus });
})();
