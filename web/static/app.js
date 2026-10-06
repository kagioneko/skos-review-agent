// Everything shown here is rendered with textContent: reference pages are
// outside content and may contain markup or instructions.
// Each response and approval button belongs to the session that produced it;
// anything from an older session is dropped, and one request runs at a time.
"use strict";
let sid = null;
let busy = false;
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };

const detail = (d) => typeof d === "string" ? d : (Array.isArray(d) ? "入力が正しくありません" : "");

async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { const e = new Error(detail((await r.json().catch(() => ({}))).detail) || r.statusText); e.status = r.status; throw e; }
  return r.json();
}

fetch("/api/info").then(r => r.json()).then(i => {
  const offline = i.mode === "offline-demo";
  $("mode").textContent = offline ? "オフラインデモモード（Gemini を呼ばず、決まった台本で動きます）" : "モデル: " + i.model;
  if (offline) { const live = document.querySelector("input[name=run][value=live]"); live.disabled = true; document.querySelector("input[name=run][value=scripted]").checked = true; }
});
document.querySelectorAll("input[name=src]").forEach(r => r.addEventListener("change", () => { $("config").hidden = r.value !== "own" || !r.checked; }));

function setBusy(b) {
  busy = b;
  for (const id of ["start", "send"]) $(id).disabled = b;
  document.querySelectorAll("#holds button").forEach(x => { x.disabled = b; });
}

async function call(path, body, forSid) {
  if (busy) return;
  setBusy(true);
  try {
    const res = await api(path, body);
    if (forSid === sid) render(res);
  } catch (e) {
    if (forSid === sid) $("timeline").append(el("li", "エラー: " + e.message, "bad"));
  } finally { setBusy(false); }
}

$("start").onclick = async () => {
  if (busy) return;
  const own = document.querySelector("input[name=src]:checked").value === "own";
  const scripted = document.querySelector("input[name=run]:checked").value === "scripted";
  setBusy(true);
  try {
    const res = await api("/api/session", { config: own ? $("config").value : null, scripted });
    sid = res.session_id;
    $("chatbox").hidden = false; $("views").hidden = false; $("timeline").replaceChildren();
    $("holds").replaceChildren(el("p", "なし", "muted"));
    $("runbanner").textContent = res.scripted
      ? "台本モード：Gemini は呼びません。仕込まれた指示に従ってしまうモデルを再生し、ゲートが止めるところを見せます。"
      : "本物の Gemini で動いています。";
    $("runbanner").hidden = false;
    render({ steps: [], gate: {}, outbox: [] });
  } catch (e) { alert("開始できません: " + e.message); }
  finally { setBusy(false); }
};

$("send").onclick = () => {
  if (!sid || busy) return;
  $("timeline").append(el("li", "あなた: " + $("msg").value));
  call("/api/chat", { session_id: sid, message: $("msg").value }, sid);
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
      const owner = sid;
      yes.onclick = () => answer(owner, h.confirmation_id, true, box); no.onclick = () => answer(owner, h.confirmation_id, false, box);
      box.append(yes, no); $("holds").append(box);
    });
  }
  const out = res.outbox || [];
  $("outbox").replaceChildren(...(out.length ? out.map(o => el("li", o.destination + " ← " + o.body)) : [el("li", "空", "muted")]));
}

// The approval card stays until the server has taken the answer: on 429/503
// nothing was used up, so the user can answer again. 409 = already answered;
// 504/500 = outcome unknown and the session has ended.
async function answer(owner, id, ok, box) {
  if (busy || owner !== sid) return;
  setBusy(true);
  try {
    const res = await api("/api/confirm", { session_id: owner, confirmation_id: id, confirmed: ok });
    if (owner !== sid) return;
    box.remove();
    $("timeline").append(el("li", ok ? "あなた: 許可しました" : "あなた: 拒否しました"));
    render(res);
  } catch (e) {
    if (owner !== sid) return;
    if (e.status !== 429 && e.status !== 503) box.remove();
    $("timeline").append(el("li", "エラー: " + e.message, "bad"));
  } finally {
    if (!$("holds").children.length) $("holds").replaceChildren(el("p", "なし", "muted"));
    setBusy(false);
  }
}
