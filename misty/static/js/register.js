// Misty POS - レジ画面
(() => {
  const root = document.getElementById("register-root");
  const UNDO_CHECKOUT_SEC = Number(root.dataset.undoSec || 60); // サーバー設定を単一ソースにする
  const seatGrid = document.getElementById("seat-grid");
  const seatMin = Number(seatGrid.dataset.min || 1);
  const seatMax = Number(seatGrid.dataset.max || 20);

  const menuGroupsEl = document.getElementById("menu-groups");
  const cartItemsEl = document.getElementById("cart-items");
  const cartTotalEl = document.getElementById("cart-total");
  const checkoutBtn = document.getElementById("checkout-btn");
  const standbyBanner = document.getElementById("standby-banner");
  const statTotal = document.getElementById("stat-total");
  const statCount = document.getElementById("stat-count");
  const undoToast = document.getElementById("undo-toast");
  const undoText = document.getElementById("undo-text");
  const undoBtn = document.getElementById("undo-btn");
  const showError = Misty.banner("error-banner");

  Misty.setDeviceLabel("レジ");

  let menu = [];
  let menuSignature = "";
  const cart = new Map(); // menu_item_id -> qty（挿入順＝カートの表示順）
  let isActive = true;
  let selectedSeat = null;
  let occupiedSeats = new Set();
  let checkingOut = false;
  // 会計ボタンを押してから成功を確認するまで同じ ID を使い回す。
  // タイムアウトで再送しても、サーバーは同じ注文を返すだけで二重計上しない
  let pendingRequestId = null;
  let undoTimer = null;

  const menuById = (id) => menu.find((m) => m.id === id);

  // ---------------------------------------------------------------- 整理番号
  // 物理的な番号札の色に合わせる（1-10: 黒、11-20: ワインレッド）
  function renderSeatGrid() {
    seatGrid.replaceChildren();
    for (let n = seatMin; n <= seatMax; n++) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "seat-btn " + (n <= 10 ? "group-a" : "group-b");
      btn.textContent = String(n);
      const occupied = occupiedSeats.has(n);
      btn.classList.toggle("occupied", occupied);
      btn.classList.toggle("selected", selectedSeat === n);
      btn.disabled = occupied;
      btn.addEventListener("click", () => {
        selectedSeat = selectedSeat === n ? null : n;
        renderSeatGrid();
        renderCart();
      });
      seatGrid.appendChild(btn);
    }
  }

  async function loadOccupiedSeats() {
    const pending = await Misty.api("/api/orders/pending");
    const next = new Set(pending.map((o) => o.seat_no));
    const changed = next.size !== occupiedSeats.size || [...next].some((n) => !occupiedSeats.has(n));
    occupiedSeats = next;
    if (selectedSeat && occupiedSeats.has(selectedSeat)) selectedSeat = null;
    if (changed) { renderSeatGrid(); renderCart(); }
  }

  // ---------------------------------------------------------------- メニュー
  function groupByCategory(items) {
    const groups = new Map(); // Map は挿入順を保つので、スプレッドシートの並び順がそのまま出る
    for (const item of items) {
      const cat = item.category || "その他";
      if (!groups.has(cat)) groups.set(cat, []);
      groups.get(cat).push(item);
    }
    return groups;
  }

  function renderMenu() {
    menuGroupsEl.replaceChildren();
    for (const [category, items] of groupByCategory(menu)) {
      const section = document.createElement("div");
      section.className = "menu-group";
      const heading = document.createElement("h3");
      heading.textContent = category;
      const grid = document.createElement("div");
      grid.className = "menu-grid";
      for (const item of items) {
        const btn = document.createElement("button");
        btn.className = "menu-card";
        btn.disabled = !item.is_active || item.stock_qty <= 0;
        const stockClass = item.stock_qty <= 0 ? "zero" : item.stock_qty <= 5 ? "low" : "";
        btn.innerHTML = `
          <div class="name">${Misty.esc(item.name)}</div>
          <div class="price">${Misty.formatYen(item.price)}</div>
          <div class="stock ${stockClass}">残り${Number(item.stock_qty)}</div>`;
        btn.addEventListener("click", () => addToCart(item.id));
        grid.appendChild(btn);
      }
      section.append(heading, grid);
      menuGroupsEl.appendChild(section);
    }
  }

  async function loadMenu() {
    const next = await Misty.api("/api/menu");
    menu = next;
    // 3秒ごとに DOM を作り直すと、押している最中のボタンが差し替わってタップが消える。
    // 内容が変わったときだけ描き直す
    const sig = JSON.stringify(next);
    if (sig !== menuSignature) {
      menuSignature = sig;
      renderMenu();
      renderCart();
    }
  }

  // ---------------------------------------------------------------- カート
  function addToCart(itemId) {
    const item = menuById(itemId);
    if (!item) return;
    const inCart = cart.get(itemId) || 0;
    if (inCart + 1 > item.stock_qty) {
      showError(`${item.name} の在庫が足りません`);
      return;
    }
    cart.set(itemId, inCart + 1);
    renderCart();
  }

  function changeQty(itemId, delta) {
    const next = (cart.get(itemId) || 0) + delta;
    if (next <= 0) cart.delete(itemId);
    else cart.set(itemId, next);
    renderCart();
  }

  function renderCart() {
    cartItemsEl.replaceChildren();
    let total = 0;
    for (const [id, qty] of cart) {
      const item = menuById(id);
      if (!item) continue;
      total += item.price * qty;
      const row = document.createElement("div");
      row.className = "cart-row";
      row.innerHTML = `
        <span>${Misty.esc(item.name)}</span>
        <span class="qty-btns">
          <button data-action="minus" aria-label="1つ減らす">−</button>
          <span class="qty">${qty}</span>
          <button data-action="plus" aria-label="1つ増やす">＋</button>
        </span>
        <span>${Misty.formatYen(item.price * qty)}</span>`;
      row.querySelector('[data-action="minus"]').addEventListener("click", () => changeQty(id, -1));
      row.querySelector('[data-action="plus"]').addEventListener("click", () => changeQty(id, 1));
      cartItemsEl.appendChild(row);
    }
    cartTotalEl.textContent = Misty.formatYen(total);
    checkoutBtn.disabled = checkingOut || !isActive || cart.size === 0 || !selectedSeat;
  }

  // ---------------------------------------------------------------- 会計
  async function postOrderWithRetry(body, attempts = 3) {
    for (let i = 0; ; i++) {
      try {
        return await Misty.api("/api/orders", { method: "POST", body: JSON.stringify(body) });
      } catch (e) {
        // 再送するのは「届いたか分からない」通信エラーだけ。400/409 はサーバーが
        // 判断した結果なので、再送しても同じ結果になる
        if (!e.network || i >= attempts - 1) throw e;
        await new Promise((r) => setTimeout(r, 500 * 2 ** i));
      }
    }
  }

  checkoutBtn.addEventListener("click", async () => {
    if (!selectedSeat || checkingOut) return;
    checkingOut = true;
    renderCart();
    pendingRequestId = pendingRequestId || Misty.newRequestId();
    const body = {
      request_id: pendingRequestId,
      seat_no: selectedSeat,
      items: [...cart].map(([menu_item_id, qty]) => ({ menu_item_id, qty })),
    };
    try {
      const order = await postOrderWithRetry(body);
      pendingRequestId = null;
      cart.clear();
      selectedSeat = null;
      showUndo(order);
      await Promise.allSettled([menuPoll.now(), summaryPoll.now(), seatPoll.now()]);
    } catch (e) {
      if (!e.network) pendingRequestId = null; // サーバーが拒否した注文は別の注文として出し直す
      showError(e.message, 6000);
    } finally {
      checkingOut = false;
      renderSeatGrid();
      renderCart();
    }
  });

  function showUndo(order) {
    clearTimeout(undoTimer);
    undoText.textContent = `整理番号${order.seat_no}の会計（${Misty.formatYen(order.total_amount)}）を取り消せます`;
    undoToast.hidden = false;
    undoBtn.onclick = async () => {
      undoBtn.disabled = true;
      try {
        await Misty.api(`/api/orders/${order.id}/void`, {
          method: "POST",
          body: JSON.stringify({ reason: "レジでの直前取消" }),
        });
        undoToast.hidden = true;
        menuPoll.now(); summaryPoll.now(); seatPoll.now();
      } catch (e) {
        showError(e.message);
      } finally {
        undoBtn.disabled = false;
      }
    };
    undoTimer = setTimeout(() => { undoToast.hidden = true; }, UNDO_CHECKOUT_SEC * 1000);
  }

  async function loadSummary() {
    const s = await Misty.api("/api/sales/summary");
    statTotal.textContent = Misty.formatYen(s.total_amount);
    statCount.textContent = s.order_count;
  }

  Misty.onStatus((s) => {
    if (isActive !== s.is_active) {
      isActive = s.is_active;
      standbyBanner.classList.toggle("show", !isActive);
      renderCart();
    }
  });

  renderSeatGrid();
  const menuPoll = Misty.poll(loadMenu, 3000);
  const summaryPoll = Misty.poll(loadSummary, 5000);
  const seatPoll = Misty.poll(loadOccupiedSeats, 2500);
  Misty.initChat("chat-panel", { role: "レジ" });
})();
