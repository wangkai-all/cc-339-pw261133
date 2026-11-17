/* Review SPA: pages, per-cell editing with optimistic locking, image
 * overlay with cell boxes, version compare and warehouse records. */

const $ = (sel) => document.querySelector(sel);
const api = async (method, url, body) => {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const resp = await fetch(url, opt);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
  return data;
};

const state = {
  docId: "receipt-1042",
  pages: [],
  current: null,
  pageDetail: null,
  revision: 0,
};

const toast = (msg, isErr) => {
  const t = $(".toast") || (() => {
    const d = document.createElement("div");
    d.className = "toast";
    document.body.appendChild(d);
    return d;
  })();
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  setTimeout(() => t.className = "toast", 2600);
};

// ------------------------------- navigation --------------------------------
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(s => s.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ------------------------------- page list ---------------------------------
async function loadPages() {
  state.docId = $("#docId").value.trim();
  const data = await api("GET", `/documents/${state.docId}/pages`);
  state.pages = data.pages || [];
  const ul = $("#pageList");
  ul.innerHTML = "";
  if (!state.pages.length) {
    ul.innerHTML = "<li>暂无校对页，先点“拉取最新抽取”</li>";
    return;
  }
  state.pages.forEach((p) => {
    const li = document.createElement("li");
    li.dataset.id = p.id;
    li.innerHTML = `版本 v${p.version} · 第 ${p.seq + 1} 页
      <span class="st">状态 ${p.status} · 修订 r${p.revision}</span>`;
    li.onclick = () => selectPage(p.id);
    ul.appendChild(li);
  });
  if (state.current && !state.pages.find(p => p.id === state.current)) {
    state.current = null;
  }
}

async function selectPage(id) {
  state.current = id;
  document.querySelectorAll("#pageList li").forEach(li =>
    li.classList.toggle("selected", Number(li.dataset.id) === id));
  const data = await api("GET", `/pages/${id}`);
  state.pageDetail = data;
  state.revision = data.page.revision;
  renderPage(data);
}

function renderPage(data) {
  const { page, cells, history } = data;
  $("#pageMeta").textContent =
    `文档 ${page.doc_id} · 版本 v${page.version} · 状态 ${page.status} · 修订 r${page.revision}`;
  $("#revBadge").textContent = "rev " + page.revision;
  $("#revBadge").classList.remove("hidden");
  $("#scanImg").src = `/pages/${page.id}/image?t=${Date.now()}`;

  const tbody = $("#cellTable tbody");
  tbody.innerHTML = "";
  cells.forEach((c) => {
    const tr = document.createElement("tr");
    const low = c.ocr_confidence < 0.75;
    tr.innerHTML = `
      <td>${c.cell_id}</td>
      <td class="${low ? "conf-low" : ""}">${escapeHtml(c.ocr_text)}
        ${low ? " ⚠" : ""}</td>
      <td>${c.ocr_confidence.toFixed(2)}</td>
      <td><input value="${escapeAttr(c.current_value)}"
        data-cell="${c.cell_id}"
        class="${c.human_edited ? "edited" : ""}"></td>
      <td><span class="pill ${c.decision}">${c.decision}</span></td>
      <td>${c.confirmed ? "已确认" : '<span class="conf-low">待确认</span>'}</td>`;
    const input = tr.querySelector("input");
    input.addEventListener("change", () => saveCell(c, input));
    tbody.appendChild(tr);
  });

  // image overlay boxes once image loads
  $("#scanImg").onload = () => drawOverlay(cells);
  drawOverlay(cells);

  $("#historyList").innerHTML = history.map(h =>
    `<li>[${h.editor}] ${h.cell_id}: "${h.old_value ?? ""}" →
     "${h.new_value ?? ""}" <em>${h.reason || ""}</em></li>`).join("") ||
    "<li>暂无历史</li>";
}

function drawOverlay(cells) {
  const img = $("#scanImg");
  const ov = $("#overlay");
  ov.innerHTML = "";
  if (!img.naturalWidth) return;
  const sx = img.clientWidth / img.naturalWidth;
  const sy = img.clientHeight / img.naturalHeight;
  cells.forEach((c) => {
    const [x0, y0, x1, y1] = c.bbox;
    const box = document.createElement("div");
    box.className = "cell-box" + (c.confirmed ? "" : " flagged");
    box.style.left = (x0 * sx) + "px";
    box.style.top = (y0 * sy) + "px";
    box.style.width = ((x1 - x0) * sx) + "px";
    box.style.height = ((y1 - y0) * sy) + "px";
    box.title = `${c.cell_id} OCR="${c.ocr_text}"`;
    ov.appendChild(box);
  });
}

async function saveCell(cell, input) {
  const value = input.value;
  try {
    const res = await api("PUT",
      `/pages/${state.current}/cells/${cell.cell_id}`, {
        value,
        expected_revision: state.revision,
        editor: $("#editor").value || "reviewer",
        reason: "网页人工校对",
      });
    state.revision = res.revision;
    input.classList.add("edited");
    toast(`已保存 ${cell.cell_id}，新修订 r${res.revision}`);
    await selectPage(state.current);
  } catch (err) {
    toast("保存失败：" + err.message, true);
    // refresh to pick up the other reviewer's revision
    await selectPage(state.current);
  }
}

$("#approveBtn").addEventListener("click", async () => {
  try {
    const res = await api("POST", `/pages/${state.current}/approve`, {
      editor: $("#editor").value || "reviewer",
    });
    toast("已送入库，记录 #" + res.warehouse.record_id);
    await loadPages();
    await selectPage(state.current);
  } catch (err) {
    toast("无法通过：" + err.message, true);
  }
});

$("#syncBtn").addEventListener("click", async () => {
  const docId = $("#docId").value.trim();
  try {
    // find current version from receiver via review sync for each known ver
    const versions = await api("GET",
      `/sync-versions?doc_id=${encodeURIComponent(docId)}`);
    for (const v of versions.versions) {
      await api("POST", "/sync", { doc_id: docId, version: v });
    }
    toast("抽取已同步到校对库");
    await loadPages();
  } catch (err) {
    toast("同步失败：" + err.message, true);
  }
});

// ------------------------------- compare -----------------------------------
$("#compareBtn").addEventListener("click", async () => {
  const docId = $("#docId").value.trim();
  const data = await api("GET",
    `/documents/${docId}/compare`);
  const out = $("#compareOut");
  if (data.note) { out.innerHTML = `<p>${data.note}</p>`; return; }
  out.innerHTML = data.comparisons.map((cmp) => `
    <div class="cmp-card">
      <h3>新 v${data.new_version} 第${cmp.new_seq + 1}页
        ${cmp.matched ? `（对应旧 v${data.old_version} 第${cmp.old_seq + 1}页，指纹匹配）`
        : "（旧版本无对应页，新增）"}</h3>
      <table><thead><tr><th>单元格</th><th>旧校对值</th><th>OCR 新值</th>
        <th>当前校对值</th><th>判定</th></tr></thead><tbody>
        ${cmp.cells.map(c => `<tr class="${c.changed ? "changed" : ""}">
          <td>${c.cell_id}</td><td>${c.old_value ?? "—"}</td>
          <td>${escapeHtml(c.ocr_value)}</td>
          <td>${escapeHtml(c.new_value)}</td>
          <td><span class="pill ${c.decision}">${c.decision}</span></td>
        </tr>`).join("")}
      </tbody></table>
    </div>`).join("");
});

// ------------------------------- warehouse ---------------------------------
$("#whBtn").addEventListener("click", async () => {
  const docId = $("#docId").value.trim();
  const data = await api("GET",
    `/documents/${docId}/warehouse-records`);
  $("#whOut").innerHTML = data.records.map(r => `
    <div class="wh-card">
      <h3>入库记录 #${r.record.id} · v${r.record.version} ·
        第${r.record.seq + 1}页 · ${r.record.approved_by}</h3>
      <table class="wh-items"><thead><tr><th>单元格</th><th>入库值</th>
        <th>来源</th></tr></thead><tbody>
        ${r.items.map(i => `<tr><td>${i.cell_id}</td>
          <td>${escapeHtml(i.value)}</td>
          <td>${i.human_edited ? "人工修改" : "OCR/复用"}</td></tr>`).join("")}
      </tbody></table>
    </div>`).join("") || "<p>暂无入库记录</p>";
});

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g,
  ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
const escapeAttr = (s) => escapeHtml(s);

loadPages().catch(err => toast(err.message, true));
