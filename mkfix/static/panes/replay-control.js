/**
 * Replay Control pane — load FIX log files, configure, and replay into a session.
 */

import { ensureMkio } from "/mkui/src/mkio-bridge.js";

const { registerPaneType } = window.Mkui;

registerPaneType("replay-control", async (spec, app, host) => {
  const client = await ensureMkio(app.config?.mkio?.url);

  host.innerHTML = `
    <div class="mkfix-toolbar">
      <button class="mkui-btn" data-action="create">Load File</button>
      <button class="mkui-btn" data-action="start" disabled>Start</button>
      <button class="mkui-btn" data-action="pause" disabled>Pause</button>
      <button class="mkui-btn" data-action="resume" disabled>Resume</button>
      <button class="mkui-btn" data-action="stop" disabled>Stop</button>
    </div>
    <div class="mkfix-replay-form" style="display:none">
      <div class="mkfix-form-row">
        <label>Name</label>
        <input type="text" data-field="name" placeholder="Replay job name">
      </div>
      <div class="mkfix-form-row">
        <label>File Path</label>
        <input type="text" data-field="file_path" placeholder="/path/to/fix.log">
      </div>
      <div class="mkfix-form-row">
        <label>Session</label>
        <select data-field="target_session"></select>
      </div>
      <div class="mkfix-form-row">
        <label>Speed</label>
        <input type="number" data-field="speed" value="1.0" step="0.1" min="0" style="max-width:80px">
        <span style="font-size:11px;color:var(--mkui-fg-mute)">× (0 = no delay)</span>
      </div>
      <div class="mkfix-form-row">
        <label>Filter</label>
        <input type="text" data-field="msg_filter" placeholder="D,8,F (empty = all)">
      </div>
      <div style="padding:4px 0 0 86px">
        <button class="mkui-btn mkfix-submit" data-action="submit-create">Create Job</button>
        <button class="mkui-btn" data-action="cancel-create">Cancel</button>
      </div>
    </div>
    <div class="mkfix-replay-jobs"></div>
  `;
  host.style.display = "flex";
  host.style.flexDirection = "column";
  host.style.overflow = "hidden";

  const toolbar = host.querySelector(".mkfix-toolbar");
  const form = host.querySelector(".mkfix-replay-form");
  const jobsEl = host.querySelector(".mkfix-replay-jobs");
  const sessionSelect = form.querySelector('[data-field="target_session"]');
  let selectedJobId = null;
  const jobsById = new Map();

  const btn = (action) => toolbar.querySelector(`[data-action="${action}"]`);

  let sessionsSubId = null;
  let jobsSubId = null;
  const sessionRows = new Map();

  function renderSessionOptions() {
    sessionSelect.innerHTML = [...sessionRows.values()]
      .map((r) => `<option value="${esc(r.session_id)}">${esc(r.session_id)}</option>`)
      .join("");
  }

  function upsertSession(op, row) {
    const key = row._mkio_row ?? row.session_id;
    if (op === "delete") sessionRows.delete(key);
    else sessionRows.set(key, row);
  }

  function upsertJob(op, row) {
    if (op === "delete") jobsById.delete(row.id);
    else jobsById.set(row.id, row);
  }

  function subscribeAll() {
    sessionsSubId = `replay-ctl-sessions-${Date.now()}`;
    client.subscribe("sessions_query", "query", {
      subid: sessionsSubId,
      onSnapshot: (rows) => {
        sessionRows.clear();
        for (const row of rows) upsertSession("insert", row);
        renderSessionOptions();
      },
      onUpdate: (op, row) => {
        upsertSession(op, row);
        renderSessionOptions();
      },
      onDelta: (changes) => {
        for (const { op, row } of changes) upsertSession(op, row);
        renderSessionOptions();
      },
    });

    jobsSubId = `replay-ctl-jobs-${Date.now()}`;
    client.subscribe("replay_jobs", "query", {
      subid: jobsSubId,
      onSnapshot: (rows) => {
        jobsById.clear();
        for (const row of rows) upsertJob("insert", row);
        renderJobs();
        updateButtons();
      },
      onUpdate: (op, row) => {
        upsertJob(op, row);
        renderJobs();
        updateButtons();
      },
      onDelta: (changes) => {
        for (const { op, row } of changes) upsertJob(op, row);
        renderJobs();
        updateButtons();
      },
    });
  }

  function unsubscribeAll() {
    if (sessionsSubId) client.unsubscribe(sessionsSubId);
    if (jobsSubId) client.unsubscribe(jobsSubId);
    sessionsSubId = null;
    jobsSubId = null;
  }

  subscribeAll();

  function jobList() {
    return [...jobsById.values()].sort((a, b) => a.id - b.id);
  }

  function renderJobs() {
    const jobs = jobList();
    if (!jobs.length) {
      jobsEl.innerHTML =
        '<div style="padding:12px;color:var(--mkui-fg-mute);font-size:12px">No replay jobs. Click "Load File" to create one.</div>';
      return;
    }
    jobsEl.innerHTML = `
      <table class="mkui-table mkfix-table" style="width:100%;font-size:12px">
        <thead><tr>
          <th>ID</th><th>Name</th><th>Session</th><th>Speed</th>
          <th>Status</th><th>Progress</th><th>Error</th>
        </tr></thead>
        <tbody>${jobs.map((j) => {
          const sel = j.id === selectedJobId ? " selected" : "";
          const pct =
            j.total_messages > 0
              ? Math.round((j.sent_messages / j.total_messages) * 100)
              : 0;
          return `<tr class="${sel}" data-id="${j.id}">
            <td>${j.id}</td>
            <td>${esc(j.name)}</td>
            <td>${esc(j.target_session)}</td>
            <td>${j.speed}×</td>
            <td><span class="mkfix-replay-status mkfix-replay-${j.status}">${j.status}</span></td>
            <td>${j.sent_messages}/${j.total_messages} (${pct}%)</td>
            <td style="color:#f44">${esc(j.error_text || "")}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    `;
  }

  function updateButtons() {
    const job = jobsById.get(selectedJobId);
    btn("start").disabled = !job || job.status === "running";
    btn("pause").disabled = !job || job.status !== "running";
    btn("resume").disabled = !job || job.status !== "paused";
    btn("stop").disabled =
      !job || (job.status !== "running" && job.status !== "paused");
  }

  jobsEl.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    selectedJobId = parseInt(tr.dataset.id);
    renderJobs();
    updateButtons();
  });

  toolbar.addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (!action) return;

    if (action === "create") {
      form.style.display = form.style.display === "none" ? "" : "none";
      return;
    }

    if (!selectedJobId) return;

    const cmd =
      action === "start"
        ? "start_replay"
        : action === "pause"
          ? "pause_replay"
          : action === "resume"
            ? "resume_replay"
            : action === "stop"
              ? "stop_replay"
              : null;

    if (cmd) {
      try {
        await client.send("fix_cmd", { command: cmd, job_id: selectedJobId }, { op: cmd });
      } catch (err) {
        console.error("Replay command failed:", err);
      }
    }
  });

  form.addEventListener("click", async (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (!action) return;

    if (action === "cancel-create") {
      form.style.display = "none";
      return;
    }

    if (action === "submit-create") {
      const data = {};
      form.querySelectorAll("[data-field]").forEach((el) => {
        data[el.dataset.field] = el.value;
      });
      if (!data.name || !data.file_path) return;
      data.speed = parseFloat(data.speed) || 1.0;

      try {
        await client.send("replay_mgmt", data, { op: "create" });
        form.style.display = "none";
      } catch (err) {
        console.error("Failed to create replay job:", err);
      }
    }
  });

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // Panes are pooled: closing a frame parks the pane and showPane() re-hosts
  // it without re-running this factory, so re-subscribe on reopen.
  const paneEl = host.closest("mkui-pane");
  paneEl?.addEventListener("mkui-pane-close", unsubscribeAll);
  paneEl?.addEventListener("mkui-pane-open", () => {
    if (!jobsSubId) subscribeAll();
  });
});
