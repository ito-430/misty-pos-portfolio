// Misty POS - 画面共通のヘルパー
//
// 方針: サーバーはホットスポット越しの1台なので、通信は「必ず失敗しうる」前提で書く。
// - fetch にはタイムアウトを付ける。付けないと iPad がホットスポットを掴み直している間、
//   リクエストが数十秒ぶら下がり、ポーリングが積み上がる。
// - ポーリングは setInterval ではなく「前回の完了後に次を予約する」方式にする。
//   応答が周期より遅いときに同じリクエストが並行して走らない。
// - 失敗が続いたら間隔を指数的に延ばし、復旧したら元に戻す。フェイルオーバー中に
//   全端末が2秒ごとに叩き続けて、昇格したばかりのスタンバイを詰まらせないため。
const Misty = (() => {
  const REQUEST_TIMEOUT_MS = 6000;

  // ---------------------------------------------------------------- 端末識別
  // 操作ログの「どの端末で操作したか」に使う。レジ用ブラウザが複数あっても区別できるよう、
  // 端末ごとに短い乱数を保存しておく（保存できない環境では毎回生成でも実害はない）
  function terminalId() {
    try {
      let id = localStorage.getItem("misty.terminal");
      if (!id) {
        id = randomHex(2).toUpperCase();
        localStorage.setItem("misty.terminal", id);
      }
      return id;
    } catch (e) {
      return randomHex(2).toUpperCase();
    }
  }

  // crypto.randomUUID() は安全なコンテキスト（HTTPS / localhost）でしか使えない。
  // LAN 内の http://192.168.137.1 では undefined になるので getRandomValues で組み立てる
  function randomHex(bytes) {
    const buf = new Uint8Array(bytes);
    crypto.getRandomValues(buf);
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function newRequestId() {
    return `${Date.now().toString(36)}-${randomHex(12)}`;
  }

  let deviceLabel = "";
  function setDeviceLabel(role) {
    deviceLabel = `${role}-${terminalId()}`;
  }

  // ---------------------------------------------------------------- 通信
  async function api(path, opts = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs || REQUEST_TIMEOUT_MS);
    const headers = { "Content-Type": "application/json" };
    // HTTP ヘッダーは Latin-1 しか通らないので、日本語の端末名は URL エンコードして送る
    if (deviceLabel) headers["X-Device-Name"] = encodeURIComponent(deviceLabel);
    let res;
    try {
      res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) }, signal: controller.signal });
    } catch (e) {
      const err = new Error(e.name === "AbortError" ? "サーバーの応答がありません" : "サーバーに接続できません");
      err.network = true; // 再送してよい種類の失敗（サーバーに届いたかどうか不明）
      throw err;
    } finally {
      clearTimeout(timer);
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 204 / 304 など本文なし */ }
    if (!res.ok) {
      const err = new Error((data && data.message) || `HTTPエラー (${res.status})`);
      err.status = res.status;
      err.code = data && data.error;
      throw err;
    }
    return data;
  }

  function poll(fn, intervalMs, { maxBackoffMs = 15000 } = {}) {
    let failures = 0;
    let timer = null;
    let inFlight = false;
    let rerun = false;

    async function tick() {
      clearTimeout(timer);
      if (inFlight) { rerun = true; return; }
      if (document.hidden) { timer = setTimeout(tick, intervalMs); return; }
      inFlight = true;
      try {
        await fn();
        failures = 0;
      } catch (e) {
        failures += 1;
      } finally {
        inFlight = false;
      }
      if (rerun) { rerun = false; return tick(); }
      const delay = failures ? Math.min(intervalMs * 2 ** failures, maxBackoffMs) : intervalMs;
      timer = setTimeout(tick, delay);
    }

    // タブ復帰・スリープ復帰の直後は、周期を待たずにすぐ最新化する
    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
    tick();
    return { now: tick };
  }

  // ---------------------------------------------------------------- 表示
  // サーバーから来る文字列（商品名・取消理由・操作ログ）は、管理画面やスプレッドシート経由で
  // 誰でも入力できる。innerHTML に入れる前に必ずエスケープする
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(v) {
    return String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
  }

  function formatYen(n) {
    return `¥${Number(n).toLocaleString("ja-JP")}`;
  }

  function formatTime(iso, { withDate = false } = {}) {
    const s = (iso || "").replace("T", " ");
    return withDate ? s.slice(0, 19) : s.slice(11, 16);
  }

  function banner(id) {
    const el = document.getElementById(id);
    let hideTimer = null;
    return (msg, ms = 4000) => {
      if (!el) return;
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => el.classList.remove("show"), ms);
    };
  }

  // ---------------------------------------------------------------- ノード状態
  const statusListeners = [];
  function onStatus(fn) { statusListeners.push(fn); }

  async function refreshStatus() {
    const s = await api("/api/status");
    const badge = document.getElementById("role-badge");
    if (badge) {
      badge.textContent = `${s.device_name} ・ ${s.is_active ? "ホスト" : "スタンバイ"}`;
      badge.classList.toggle("standby", !s.is_active);
    }
    const cluster = document.getElementById("cluster-banner");
    if (cluster) {
      let msg = "";
      if (s.split_brain) msg = "ホストが2台動いています。旧ホスト機を停止中です。会計は続けて大丈夫です";
      else if (s.fenced_by_epoch) msg = "この端末は新しいホストに切り替わりました。接続画面のQRから新しいホストに繋ぎ直してください";
      cluster.textContent = msg;
      cluster.classList.toggle("show", !!msg);
    }
    statusListeners.forEach((fn) => fn(s));
  }

  // ---------------------------------------------------------------- チャット
  // role は「レジ」「バーテン」のように持ち場を表す表示ラベル
  function initChat(containerId, { role = "端末" } = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const messagesEl = container.querySelector(".chat-messages");
    const inputEl = container.querySelector(".chat-input");
    const sendBtn = container.querySelector(".chat-send-btn");
    if (!messagesEl || !inputEl || !sendBtn) return;

    let lastId = 0;

    function render(messages) {
      const nearBottom = messagesEl.scrollTop + messagesEl.clientHeight >= messagesEl.scrollHeight - 16;
      for (const m of messages) {
        if (m.id <= lastId) continue; // 送信直後の poll と定期 poll が重なっても二重表示しない
        const row = document.createElement("div");
        row.className = "chat-msg" + (m.sender === role ? " mine" : "");
        const meta = document.createElement("div");
        meta.className = "chat-meta";
        meta.textContent = `${m.sender} ・ ${formatTime(m.ts)}`;
        const textEl = document.createElement("div");
        textEl.className = "chat-text";
        textEl.textContent = m.text;
        row.append(meta, textEl);
        messagesEl.appendChild(row);
        lastId = m.id;
      }
      if (messages.length && nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    const poller = poll(async () => render(await api(`/api/chat?after=${lastId}`)), 3000);

    async function send() {
      const text = inputEl.value.trim();
      if (!text) return;
      inputEl.value = "";
      try {
        await api("/api/chat", { method: "POST", body: JSON.stringify({ text, sender: role }) });
        poller.now();
      } catch (e) {
        inputEl.value = text; // 送信に失敗したら入力内容を戻し、打ち直させない
      }
    }

    sendBtn.addEventListener("click", send);
    inputEl.addEventListener("keydown", (e) => {
      // 日本語入力の変換確定の Enter で誤送信しない
      if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); send(); }
    });
  }

  poll(refreshStatus, 5000);

  return { api, poll, esc, formatYen, formatTime, banner, initChat, onStatus, newRequestId, setDeviceLabel };
})();
