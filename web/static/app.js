// Everything shown here is rendered with textContent: reference pages are
// outside content and may contain markup or instructions.
"use strict";
let sid = null;
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };

async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

fetch("/api/info").then(r => r.json()).then(i => {
  $("mode").textContent = i.mode === "offline-demo" ? "オフラインデモモード（Gemini を呼ばず、決まった台本で動きます）" : "モデル: " + i.model;
});
document.querySelectorAll("input[name=src]").forEach(r => r.addEventListener("change", () => { $("config").hidden = r.value !== "own" || !r.checked; }));

$("start").onclick = async () => {
  const own = document.querySelector("input[name=src]:checked").value === "own";
  try {
    const res = await api("/api/session", { config: own ? $("config").value : null });
    sid = res.session_id;
    $("chatbox").hidden = false; $("views").hidden = false; $("timeline").replaceChildren(); render({ steps: [], gate: {}, outbox: [] });
  } catch (e) { alert("開始できません: " + e.message); }
};

$("send").onclick = async () => {
  if (!sid) return;
  $("timeline").append(el("li", "あなた: " + $("msg").value));
  try { render(await api("/api/chat", { session_id: sid, message: $("msg").value })); }
  catch (e) { $("timeline").append(el("li", "エラー: " + e.message, "bad")); }
};

function describe(step) {
  const li = el("li");
  if (step.kind === "call") { li.append(el("span", "ツール呼び出し: ")); li.append(el("span", step.tool, "tool")); li.append(el("pre", JSON.stringify(step.args, null, 2))); }
  else if (step.kind === "result") { li.append(el("span", "結果: ")); li.append(el("span", step.tool, "tool")); li.append(el("pre", JSON.stringify(step.result, null, 2))); }
  else if (step.kind === "hold") { li.append(el("strong", "ゲートが止めました: " + step.tool, "bad")); }
  else if (step.kind === "text") { li.append(el("span", (step.author === "user" ? "あなた: " : "エージェント: ") + step.text)); }
  return li;
}

function render(res) {
  for (const s of res.steps) $("timeline").append(describe(s));
  const g = res.gate || {};
  $("gate").replaceChildren(
    el("li", "外部の内容を読んだ: " + (g.outside_content_read ? "はい" : "いいえ"), g.outside_content_read ? "bad" : "ok"),
    el("li", "機密（あなたの設定）を読んだ: " + (g.private_data_read ? "はい" : "いいえ"), g.private_data_read ? "bad" : "ok"));
  const holds = res.steps.filter(s => s.kind === "hold");
  const reasons = (g.holds || []);
  if (holds.length) {
    $("holds").replaceChildren();
    holds.forEach((h, i) => {
      const box = el("div", undefined, "hold");
      box.append(el("strong", "承認待ち: " + h.tool));
      const reason = reasons[reasons.length - holds.length + i];
      if (reason) box.append(el("p", reason.reason));
      box.append(el("pre", JSON.stringify(h.args, null, 2)));
      const yes = el("button", "許可する"), no = el("button", "拒否する", "secondary");
      yes.onclick = () => answer(h.confirmation_id, true, box); no.onclick = () => answer(h.confirmation_id, false, box);
      box.append(yes, no); $("holds").append(box);
    });
  }
  const out = res.outbox || [];
  $("outbox").replaceChildren(...(out.length ? out.map(o => el("li", o.destination + " ← " + o.body)) : [el("li", "空", "muted")]));
}

async function answer(id, ok, box) {
  box.remove();
  if (!$("holds").children.length) $("holds").replaceChildren(el("p", "なし", "muted"));
  $("timeline").append(el("li", ok ? "あなた: 許可しました" : "あなた: 拒否しました"));
  try { render(await api("/api/confirm", { session_id: sid, confirmation_id: id, confirmed: ok })); }
  catch (e) { $("timeline").append(el("li", "エラー: " + e.message, "bad")); }
}
