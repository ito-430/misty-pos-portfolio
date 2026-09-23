// Misty POS - バーテン画面
(() => {
  const root = document.getElementById("bartender-root");
  const UNDO_COMPLETE_SEC = Number(root.dataset.undoSec || 30);

  const taskGrid = document.getElementById("task-grid");
  const emptyState = document.getElementById("empty-state");
  const donePanel = document.getElementById("done-panel");
  const doneTbody = document.getElementById("done-tbody");
  const toggleDoneBtn = document.getElementById("toggle-done-btn");
  const undoToast = document.getElementById("undo-toast");
  const undoText = document.getElementById("undo-text");
  const undoBtn = document.getElementById("undo-btn");
  const showError = Misty.banner("error-banner");

  Misty.setDeviceLabel("バーテン");

  let undoTimer = null;
  let knownIds = null; // 初回読み込みの注文は「新着」扱いしない
  let lastSignature = "";

  const itemsLabel = (order) =>
    order.items.map((i) => `${Misty.esc(i.menu_item_name)} ×${Number(i.qty)}`).join(" / ");

  function showUndo(order) {
    clearTimeout(undoTimer);
    undoText.textContent = `整理番号${order.seat_no}を「完了」にしました`;
    undoToast.hidden = false;
    undoBtn.onclick = async () => {
      try {
        await Misty.api(`/api/orders/${order.id}/uncomplete`, { method: "POST" });
        undoToast.hidden = true;
        pendingPoll.now();
      } catch (e) { showError(e.message); }
    };
    undoTimer = setTimeout(() => { undoToast.hidden = true; }, UNDO_COMPLETE_SEC * 1000);
  }

  async function complete(order, button) {
    button.disabled = true; // 二度押しで2回目が「準備中ではありません」エラーになるのを防ぐ
    try {
      await Misty.api(`/api/orders/${order.id}/complete`, { method: "POST" });
      showUndo(order);
    } catch (e) {
      // 別のバーテン端末が先に完了にした場合も 409 になる。一覧を更新すれば消えるので伝えるだけ
      showError(e.message);
      if (e.network) button.disabled = false; // 届かなかっただけなら押し直せるようにする
    }
    pendingPoll.now();
  }

  function renderTasks(orders) {
    const currentIds = new Set(orders.map((o) => o.id));
    if (knownIds && [...currentIds].some((id) => !knownIds.has(id))) {
      // 手元を見ながら作業しているバーテンが新着に気付けるよう、振動（対応端末のみ）と
      // 新着カードの強調表示で知らせる
      if (navigator.vibrate) navigator.vibrate(150);
    }
    const fresh = knownIds ? new Set([...currentIds].filter((id) => !knownIds.has(id))) : new Set();
    knownIds = currentIds;

    const sig = JSON.stringify(orders.map((o) => [o.id, o.status]));
    if (sig === lastSignature) return; // 変化がなければ DOM に触らない（タップ中のボタンを守る）
    lastSignature = sig;

    taskGrid.replaceChildren();
    emptyState.hidden = orders.length !== 0;
    for (const order of orders) {
      const card = document.createElement("div");
      card.className = "task-card" + (fresh.has(order.id) ? " fresh" : "");
      card.innerHTML = `
        <div class="seat">No.${Number(order.seat_no)}</div>
        <ul>${order.items.map((i) => `<li>${Misty.esc(i.menu_item_name)} ×${Number(i.qty)}</li>`).join("")}</ul>
        <button class="complete">完了</button>`;
      const btn = card.querySelector("button.complete");
      btn.addEventListener("click", () => complete(order, btn));
      taskGrid.appendChild(card);
    }
  }

  async function loadDone() {
    if (donePanel.hidden) return;
    const orders = await Misty.api("/api/orders/recent?status=done&limit=50");
    doneTbody.replaceChildren();
    for (const order of orders) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${Number(order.seat_no)}</td>
        <td>${itemsLabel(order)}</td>
        <td>${Misty.esc(Misty.formatTime(order.completed_at, { withDate: true }))}</td>
        <td><button class="secondary">未完了に戻す</button></td>`;
      tr.querySelector("button").addEventListener("click", async () => {
        try {
          await Misty.api(`/api/orders/${order.id}/uncomplete`, { method: "POST" });
          pendingPoll.now();
          donePoll.now();
        } catch (e) { showError(e.message); }
      });
      doneTbody.appendChild(tr);
    }
  }

  toggleDoneBtn.addEventListener("click", () => {
    donePanel.hidden = !donePanel.hidden;
    toggleDoneBtn.textContent = donePanel.hidden ? "完了済み一覧を表示" : "完了済み一覧を隠す";
    donePoll.now();
  });

  const pendingPoll = Misty.poll(async () => renderTasks(await Misty.api("/api/orders/pending")), 2000);
  const donePoll = Misty.poll(loadDone, 4000);
  Misty.initChat("chat-panel", { role: "バーテン" });
})();
