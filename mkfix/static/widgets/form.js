/**
 * Form widgets for mkui: input, select, checkbox.
 * Registered via mkui's registerWidget() extensibility point.
 */

const { registerWidget } = window.Mkui;

/* ── input ────────────────────────────────────────────────────────── */

registerWidget("input", (spec, app, host) => {
  const wrap = document.createElement("div");
  wrap.className = "mkfix-field";

  if (spec.label) {
    const label = document.createElement("label");
    label.className = "mkfix-label";
    label.textContent = spec.label;
    wrap.appendChild(label);
  }

  const input = document.createElement("input");
  input.className = "mkfix-input";
  input.type = spec.inputType || "text";
  if (spec.placeholder) input.placeholder = spec.placeholder;
  if (spec.readonly) input.readOnly = true;
  if (spec.min !== undefined) input.min = spec.min;
  if (spec.max !== undefined) input.max = spec.max;
  if (spec.step) input.step = spec.step;

  if (spec.bind) {
    const current = app.state.get(spec.bind);
    if (current != null) input.value = current;
    app.state.subscribe(spec.bind, (v) => {
      if (document.activeElement !== input) input.value = v ?? "";
    });
    input.addEventListener("input", () => {
      const val = input.type === "number" ? (input.value === "" ? null : Number(input.value)) : input.value;
      app.state.set(spec.bind, val);
    });
  }

  wrap.appendChild(input);
  host.appendChild(wrap);
});

/* ── select ───────────────────────────────────────────────────────── */

registerWidget("select", (spec, app, host) => {
  const wrap = document.createElement("div");
  wrap.className = "mkfix-field";

  if (spec.label) {
    const label = document.createElement("label");
    label.className = "mkfix-label";
    label.textContent = spec.label;
    wrap.appendChild(label);
  }

  const select = document.createElement("select");
  select.className = "mkfix-select";

  function populateOptions(options) {
    select.innerHTML = "";
    if (spec.placeholder) {
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = spec.placeholder;
      ph.disabled = true;
      ph.selected = true;
      select.appendChild(ph);
    }
    for (const opt of options || []) {
      const o = document.createElement("option");
      if (typeof opt === "string") {
        o.value = opt;
        o.textContent = opt;
      } else {
        o.value = opt.value;
        o.textContent = opt.label || opt.value;
      }
      select.appendChild(o);
    }
  }

  populateOptions(spec.options);

  if (spec.bindOptions) {
    app.state.subscribe(spec.bindOptions, (opts) => {
      const current = select.value;
      populateOptions(opts || []);
      select.value = current;
    });
  }

  if (spec.bind) {
    const current = app.state.get(spec.bind);
    if (current != null) select.value = current;
    app.state.subscribe(spec.bind, (v) => {
      if (v != null) select.value = v;
    });
    select.addEventListener("change", () => {
      app.state.set(spec.bind, select.value);
    });
  }

  wrap.appendChild(select);
  host.appendChild(wrap);
});

/* ── checkbox ─────────────────────────────────────────────────────── */

registerWidget("checkbox", (spec, app, host) => {
  const wrap = document.createElement("div");
  wrap.className = "mkfix-field mkfix-field-check";

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "mkfix-checkbox";

  if (spec.label) {
    const label = document.createElement("label");
    label.className = "mkfix-label-inline";
    label.appendChild(cb);
    label.append(" " + spec.label);
    wrap.appendChild(label);
  } else {
    wrap.appendChild(cb);
  }

  if (spec.bind) {
    const current = app.state.get(spec.bind);
    cb.checked = !!current;
    app.state.subscribe(spec.bind, (v) => { cb.checked = !!v; });
    cb.addEventListener("change", () => {
      app.state.set(spec.bind, cb.checked);
    });
  }

  host.appendChild(wrap);
});
