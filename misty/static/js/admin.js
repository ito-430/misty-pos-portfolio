// Misty POS - 管理画面
(() => {
  const menuTbody = document.getElementById("menu-tbody");
  const rankingTbody = document.getElementById("ranking-tbody");
  const ordersTbody = document.getElementById("orders-tbody");
  const logTbody = document.getElementById("log-tbody");
  const statTotal = document.getElementById("stat-total");
  const statCount = document.getElementById("stat-count");
  const showError = Misty.banner("error-banner");
  const showInfo = Misty.banner("info-banner");
  const esc = Misty.esc;

  Misty.setDeviceLabel("管理");

  const put = (id, body) =>
    Misty.api(`/api/menu/${id}`, { method: "PUT", body: JSON.stringify(body) });

  // ---------------------------------------------------------------- メニュー
  async function loadMenu() {
    const menu = await Misty.api("/api/menu");
    menuTbody.replaceChildren();
    for (const item of menu) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(item.name)}</td>
        <td>${esc(item.category)}</td>
        <td><input type="number" min="0" step="1" inputmode="numeric" value="${Number(item.price)}" data-field="price" class="num-input"></td>
        <td><input type="number" min="0" step="1" inputmode="numeric" value="${Number(item.stock_qty)}" data-field="stock_qty" class="num-input"></td>
        <td><button class="secondary" data-field="is_active">${item.is_active ? "販売中" : "停止中"}</button></td>`;
      tr.querySelectorAll("input").forEach((input) => {
        input.addEventListener("change", async () => {
          const prev = input.defaultValue;
          try {
            await put(item.id, { [input.dataset.field]: input.value });
            input.defaultValue = input.value;
            showInfo(`${item.name} を更新しました`);
          } catch (e) {
            input.value = prev; // サーバーが拒否した値を画面に残さない
            showError(e.message);
          }
        });
      });
      tr.querySelector('button[data-field="is_active"]').addEventListener("click", async () => {
        try {
          await put(item.id, { is_active: item.is_active ? 0 : 1 });
          loadMenu();
        } catch (e) { showError(e.message); }
      });
      menuTbody.appendChild(tr);
    }
  }

  const val = (id) => document.getElementById(id).value.trim();

  document.getElementById("add-item-btn").addEventListener("click", async () => {
    const name = val("new-name");
    if (!name) { showError("商品名を入力してください"); return; }
    try {
      await Misty.api("/api/menu", {
        method: "POST",
        body: JSON.stringify({
          name, category: val("new-category"), price: val("new-price") || 0, stock_qty: val("new-stock") || 0,
        }),
      });
      ["new-name", "new-category", "new-price", "new-stock"].forEach((id) => { document.getElementById(id).value = ""; });
      loadMenu();
    } catch (e) { showError(e.message); }
  });

  document.getElementById("import-btn").addEventListener("click", async (ev) => {
    const url = val("sheet-url");
    if (!url) { showError("スプレッドシートのURLを入力してください"); return; }
    ev.target.disabled = true;
    try {
      const r = await Misty.api("/api/menu/import", {
        method: "POST", body: JSON.stringify({ sheet_url: url }), timeoutMs: 40000,
      });
      let msg = `取込完了: 新規${r.created}件・更新${r.updated}件`;
      if (r.skipped.length) {
        msg += `／読み飛ばし${r.skipped.length}件（` +
          r.skipped.slice(0, 3).map((s) => `${s.row}行目: ${s.reason}`).join("、") + "）";
      }
      showInfo(msg, 10000);
      loadMenu();
    } catch (e) {
      showError(e.message, 10000);
    } finally {
      ev.target.disabled = false;
    }
  });

  // ---------------------------------------------------------------- 集計
  async function loadSummary() {
    const s = await Misty.api("/api/sales/summary");
    statTotal.textContent = Misty.formatYen(s.total_amount);
    statCount.textContent = s.order_count;
    rankingTbody.innerHTML = s.ranking
      .map((r) => `<tr><td>${esc(r.name)}</td><td>${Number(r.qty)}</td><td>${Misty.formatYen(r.amount)}</td></tr>`)
      .join("");
  }

  // ---------------------------------------------------------------- 注文履歴・取消
  const statusLabel = { pending: "準備中", done: "完了", void: "取消済み" };
  let editingOrderId = null; // 取消理由を入力中の行は、定期更新で消さない

  async function loadOrders() {
    if (editingOrderId !== null) return;
    const orders = await Misty.api("/api/orders/recent?limit=50");
    ordersTbody.replaceChildren();
    for (const o of orders) {
      const tr = document.createElement("tr");
      const canVoid = o.status !== "void";
      tr.innerHTML = `
        <td>${Number(o.id)}</td><td>${Number(o.seat_no)}</td><td>${esc(statusLabel[o.status] || o.status)}</td>
        <td>${o.items.map((i) => `${esc(i.menu_item_name)}×${Number(i.qty)}`).join(" / ")}</td>
        <td>${Misty.formatYen(o.total_amount)}</td>
        <td>${esc(Misty.formatTime(o.created_at, { withDate: true }))}</td>
        <td>${canVoid ? '<button class="secondary danger" data-action="void">取消</button>' : esc(o.void_reason || "")}</td>`;
      if (canVoid) tr.querySelector('[data-action="void"]').addEventListener("click", () => startVoid(o, tr));
      ordersTbody.appendChild(tr);
    }
  }

  function startVoid(order, tr) {
    editingOrderId = order.id;
    const cell = tr.lastElementChild;
    cell.innerHTML = `
      <input type="text" placeholder="取消理由" maxlength="200" class="reason-input">
      <button class="secondary danger" data-action="confirm">確定</button>
      <button class="secondary" data-action="cancel">やめる</button>`;
    const done = () => { editingOrderId = null; ordersPoll.now(); };
    cell.querySelector('[data-action="cancel"]').addEventListener("click", done);
    cell.querySelector('[data-action="confirm"]').addEventListener("click", async () => {
      const reason = cell.querySelector("input").value.trim();
      try {
        await Misty.api(`/api/orders/${order.id}/void`, { method: "POST", body: JSON.stringify({ reason }) });
        showInfo(`注文#${order.id}を取り消しました`);
        loadMenu();
        summaryPoll.now();
      } catch (e) { showError(e.message); }
      done();
    });
    cell.querySelector("input").focus();
  }

  // ---------------------------------------------------------------- 操作ログ
  async function loadLog() {
    const rows = await Misty.api("/api/logs?limit=200");
    logTbody.innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(Misty.formatTime(r.ts, { withDate: true }))}</td>
        <td>${esc(r.device_name)}</td><td>${esc(r.action)}</td>
        <td class="mono">${esc(r.detail)}</td>
      </tr>`).join("");
  }

  loadMenu().catch((e) => showError(e.message));
  const summaryPoll = Misty.poll(loadSummary, 5000);
  const ordersPoll = Misty.poll(loadOrders, 5000);
  Misty.poll(loadLog, 10000);
})();
