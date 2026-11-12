// The review screen. Plain TypeScript, no framework: it lists the invoices held for a person,
// shows one in full, and sends the decision to the API (web/app.py). It also lists what was
// resolved without a person (paid or rejected by the policy, skipped as a duplicate) and shows
// each of those with the same evidence, read-only.
//
// Invoice text is written by the vendor. Everything is put on the page with textContent, never
// as HTML, so nothing a vendor writes can become markup or script.

interface Summary {
  id: number; file: string; vendor: string | null; total: string | null; currency: string;
  usd_estimate: string | null; reasons: string[]; recommendation: string | null; has_saved_run: boolean;
  stuck?: { action: string; reviewer: string; note?: string | null } | false;
}
interface Flag { code: string; severity: string; meaning: string; detail: Record<string, unknown>; }
interface Advice {
  by: string; recommendation: string | null; rationale: string; open_questions: string[]; note: string | null;
  lookups: { tool: string; arguments: unknown; result: unknown }[]; verified: boolean;
}
interface Resolved {
  status: string; by: string; who: string; reviewer: string | null; note: string | null; rule: string | null;
  amount: string | null; currency: string | null; error: string | null;
}
interface Automatic {
  run: string; file: string; vendor: string | null; total: string | null; currency: string; status: string;
  by: string; who: string; reasons: string[]; note: string | null;
}
interface Detail extends Omit<Summary, "id"> {
  id: number | null; resolved: Resolved | null;
  status: string; invoice_number: string | null; date: string | null; due_date: string | null;
  payment_terms: string | null; notes: string | null; subtotal: string | null; tax_amount: string | null; shipping: string | null;
  line_items: { item: string | null; quantity: string | null; unit_price: string | null; note: string | null }[];
  flags: Flag[]; pays: { amount: string | null; currency: string; already_paid: string | null } | null;
  cannot_approve: string | null; conversion: { usd_estimate: string; as_of?: string } | null;
  advice: Advice | null; trail: unknown[];
}
interface Decided {
  id: number; file: string; vendor: string | null; total: string | null; status: string;
  action: string | null; reviewer: string | null; note: string | null;
  payment: { amount: string; currency: string; status: string } | null;
}
interface Outcome {
  file: string; vendor: string | null;
  result: { status: string; amount?: string; currency?: string; error?: string; reasons?: string[] };
}
interface Queue { waiting: Summary[]; decided: Decided[]; automatic: Automatic[]; paid: Record<string, string>; payments: number; }

const $ = (id: string) => document.getElementById(id) as HTMLElement;
let selected: number | null = null;      // the waiting invoice shown, by queue number
let selectedRun: string | null = null;   // or the finished run shown
let shownOnce = false;                   // the first invoice opens by itself; don't scroll the page for that one
const OUTCOME: Record<string, string> = {
  PAID: "Paid", REJECTED: "Rejected", SKIPPED_DUPLICATE: "Skipped: duplicate", SKIPPED_SUPERSEDED: "Skipped: superseded",
  ERROR: "Could not be read", PAYMENT_UNCONFIRMED: "Payment not confirmed", PAYMENT_NOT_SENT: "Payment not sent",
};
const outcomeClass = (status: string) => status === "PAID" ? "approve" : status.startsWith("SKIPPED") ? "" : "reject";
let waiting: Summary[] = [];
let latest = 0;   // counts requests for an invoice's detail, so an answer that arrives late is dropped

/** Create an element. Children that are strings become text nodes. */
function el(tag: string, attrs: Record<string, string> = {}, ...children: (Node | string | null)[]): HTMLElement {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const child of children) if (child !== null) node.append(child);
  return node;
}

function money(amount: string | null, currency = "USD"): string {
  if (amount === null || amount === "") return "–";
  const n = Number(amount);
  if (Number.isNaN(n)) return amount;
  const text = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const sign = n < 0 ? "-" : "";
  return currency === "USD" ? `${sign}$${text}` : `${sign}${text} ${currency}`;
}

function amountOf(s: { total: string | null; currency: string; usd_estimate: string | null }): string {
  const own = money(s.total, s.currency);
  return s.currency === "USD" || s.usd_estimate === null ? own : `${own} (about ${money(s.usd_estimate)})`;
}

/** A flag code as words: STOCK_EXCEEDED -> "Stock exceeded". */
function words(code: string): string {
  const text = code.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(typeof problem.detail === "string" ? problem.detail : `The server answered ${response.status}.`);
  }
  return response.json() as Promise<T>;
}

function showProblem(message: string | null): void {
  $("problem").hidden = message === null;
  $("problem").textContent = message ?? "";
}

async function loadQueue(openFirst = true): Promise<void> {
  let queue: Queue;
  try {
    queue = await api<Queue>("/api/queue");
    showProblem(null);
  } catch (e) {
    showProblem((e as Error).message);
    return;
  }
  const paid = Object.entries(queue.paid).map(([currency, amount]) => money(amount, currency)).join(" + ") || "$0.00";
  $("totals").textContent = `${queue.payments} payment${queue.payments === 1 ? "" : "s"} made: ${paid}`;
  $("count").textContent = String(queue.waiting.length);
  waiting = queue.waiting;

  const rows = queue.waiting.map((s) => {
    const button = el("button", { type: "button", "aria-current": String(s.id === selected) },
      el("div", { class: "row" }, el("strong", {}, s.vendor ?? "No vendor named"), el("span", {}, money(s.total, s.currency))),
      el("div", { class: "small" }, s.file),
      el("div", { class: "small" }, s.stuck ? `Decided (${s.stuck.action} by ${s.stuck.reviewer}) but not carried out`
        : !s.has_saved_run ? "Saved run not found" : s.reasons.map(words).join(", ")));
    button.dataset.id = String(s.id);
    button.addEventListener("click", () => select(s.id));
    return el("li", {}, button);
  });
  $("waiting").replaceChildren(...(rows.length ? rows : [el("li", { class: "empty" }, "Nothing is waiting.")]));

  const decided = queue.decided.map((d) => el("li", { class: "done" },
    el("div", { class: "row" }, el("strong", {}, d.vendor ?? "No vendor named"),
      el("span", { class: `tag ${d.action ?? ""}` }, d.action === "approve" ? "Approved" : d.action === "reject" ? "Rejected" : d.status)),
    el("div", { class: "small" }, `${d.file} · ${d.reviewer ?? "nobody"}`
      + (d.payment?.status === "PAID" ? ` · paid ${money(d.payment.amount, d.payment.currency)}` : "")),
    d.payment && d.payment.status !== "PAID"
      ? el("div", { class: "small warn" }, `Payment of ${money(d.payment.amount, d.payment.currency)} was sent but not confirmed: check with the bank`) : null,
    d.note ? el("div", { class: "small" }, `“${d.note}”`) : null));
  $("decided").replaceChildren(...(decided.length ? decided : [el("li", { class: "empty" }, "No decisions yet.")]));

  $("automatic-count").textContent = String(queue.automatic.length);
  const automatic = queue.automatic.map((a) => {
    const button = el("button", { type: "button", "aria-current": String(a.run === selectedRun) },
      el("div", { class: "row" }, el("strong", {}, a.vendor ?? a.file), el("span", { class: `tag ${outcomeClass(a.status)}` }, OUTCOME[a.status] ?? a.status)),
      el("div", { class: "small" }, `${a.file} · ${money(a.total, a.currency)} · ${a.who}`),
      el("div", { class: "small" }, a.note ?? (a.reasons.length ? a.reasons.map(words).join(", ") : "Nothing to object to")));
    button.dataset.run = a.run;
    button.addEventListener("click", () => selectRun(a.run));
    return el("li", {}, button);
  });
  $("automatic").replaceChildren(...(automatic.length ? automatic : [el("li", { class: "empty" }, "Nothing yet.")]));

  if (selected !== null && !queue.waiting.some((s) => s.id === selected)) selected = null;   // decided elsewhere
  if (selected === null && selectedRun === null && openFirst && queue.waiting.length) await select(queue.waiting[0].id);
  if (selected === null && selectedRun === null && openFirst) $("detail").replaceChildren(el("p", { class: "empty" }, "Nothing is waiting for a decision."));
}

function highlight(): void {
  document.querySelectorAll<HTMLElement>("#waiting button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === String(selected))));
  document.querySelectorAll<HTMLElement>("#automatic button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.run === selectedRun)));
}

/** Show a run that finished without a person: the same evidence as a held invoice, and how it ended in place of the form. */
async function selectRun(run: string): Promise<void> {
  selected = null;
  selectedRun = run;
  const request = ++latest;
  highlight();
  try {
    const d = await api<Detail>(`/api/runs/${encodeURIComponent(run)}`);
    if (request === latest) render(d);
  } catch (e) {
    if (request === latest) $("detail").replaceChildren(el("p", { class: "notice error" }, (e as Error).message));
  }
}

async function select(id: number): Promise<void> {
  selected = id;
  selectedRun = null;
  const request = ++latest;
  highlight();
  const summary = waiting.find((s) => s.id === id);
  if (summary && !summary.has_saved_run) { renderLost(summary); return; }
  let d: Detail;
  try {
    d = await api<Detail>(`/api/queue/${id}`);
  } catch (e) {
    if (request !== latest) return;
    $("detail").replaceChildren(el("p", { class: "notice error" }, (e as Error).message));
    selected = null;
    await loadQueue(false);   // the list was out of date (someone else decided this one): refresh it, keep the message
    return;
  }
  if (request !== latest) return;   // another invoice was chosen while this one loaded
  render(d);
}

/** A waiting invoice whose paused run can't be found: it can't be decided, only put back to be processed again. */
function renderLost(s: Summary): void {
  const button = el("button", { type: "button", class: "reject" }, "Put it back to be processed again") as HTMLButtonElement;
  const message = el("p", { class: "notice error" }, `The saved run for ${s.file} was not found, so it can't be approved or rejected here. `
    + "Putting it back closes this entry; run main.py on the file again and it will be checked and queued afresh.");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/queue/${s.id}/reopen`, {});
      showOutcome(`${s.file} was put back as unprocessed. Run main.py on it again.`, "notice");
      selected = null;
      await loadQueue();
    } catch (e) {
      message.textContent = (e as Error).message;
      button.disabled = false;
    }
  });
  $("detail").replaceChildren(el("h2", {}, s.vendor ?? "No vendor named"), el("div", { class: "amount" }, amountOf(s)), message,
    el("form", {}, el("div", { class: "buttons" }, button)));
}

function showOutcome(message: string | null, kind = "notice done"): void {
  $("outcome").hidden = message === null;
  $("outcome").className = kind;
  $("outcome").textContent = message ?? "";
}

/** What a decision did, in words. A payment that didn't confirm, or wasn't sent, is not reported as paid. */
function describe(o: Outcome): [string, string] {
  const r = o.result, who = o.vendor ?? o.file;
  const amount = money(r.amount ?? null, r.currency ?? "USD");
  if (r.status === "PAID") return [`Paid ${amount} to ${who} (${o.file}).`, "notice done"];
  if (r.status === "REJECTED") return [`Rejected ${o.file}: ${(r.reasons ?? []).join("; ")}`, "notice done"];
  if (r.status === "PAYMENT_UNCONFIRMED") {
    return [`The payment of ${amount} to ${who} was sent but did not confirm${r.error ? ` (${r.error})` : ""}. `
      + "It may or may not have gone through: check with the bank. It will not be sent again automatically.", "notice error"];
  }
  if (r.status === "PAYMENT_NOT_SENT") {
    return [`Nothing was paid to ${who}: ${r.error ?? "the payment could not be sent"}. ${o.file} was put back to be processed again.`, "notice error"];
  }
  return [`${o.file}: ${r.status}`, "notice"];
}

function render(d: Detail): void {
  const held = d.flags.filter((f) => f.severity !== "INFO");
  const notes = d.flags.filter((f) => f.severity === "INFO");
  const flagList = (flags: Flag[]) => el("ul", { class: "flags" }, ...flags.map((f) => el("li", {},
    el("span", { class: `tag ${f.severity}` }, f.severity === "REVIEW" ? (d.resolved ? "Review" : "Held") : f.severity === "WARN" ? "Warning" : f.severity === "BLOCK" ? "Blocks" : "Note"),
    el("div", {}, el("strong", {}, words(f.code)), `: ${f.meaning}`,
      el("div", { class: "small" }, Object.entries(f.detail).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(" · "))))));

  const lines = el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Item"), el("th", { class: "n" }, "Quantity"), el("th", { class: "n" }, "Unit price"), el("th", {}, "Note"))),
    el("tbody", {}, ...d.line_items.map((li) => el("tr", {},
      el("td", {}, li.item ?? "–"), el("td", { class: "n" }, li.quantity ?? "–"),
      el("td", { class: "n" }, money(li.unit_price, d.currency)), el("td", {}, li.note ?? "")))));

  const a = d.advice;
  const advice = a === null ? null : el("div", { class: "advice" },
    el("div", {}, el("span", { class: `tag ${a.recommendation ?? ""}` }, a.by === "reviewer_agent" ? `Agent recommends ${a.recommendation}` : "Written by the policy"),
      a.by === "reviewer_agent" && !a.verified ? el("span", { class: "tag WARN" }, "Not verified by the critic") : null),
    el("p", {}, a.rationale),
    a.open_questions.length ? el("div", {}, el("strong", {}, "To confirm before deciding"), el("ul", {}, ...a.open_questions.map((q) => el("li", {}, q)))) : null,
    a.lookups.length ? el("details", {}, el("summary", {}, `What the agent looked up (${a.lookups.length})`),
      el("pre", {}, a.lookups.map((l) => `${l.tool}(${JSON.stringify(l.arguments)})\n  → ${JSON.stringify(l.result)}`).join("\n"))) : null);

  const pays = d.pays === null ? "" : d.pays.already_paid !== null
    ? `Approving pays ${money(d.pays.amount, d.pays.currency)}: the difference. ${money(d.pays.already_paid, d.pays.currency)} was already paid on the earlier version.`
    : `Approving pays ${money(d.pays.amount, d.pays.currency)}${d.conversion ? `, about ${money(d.conversion.usd_estimate)} at the rate service's rate` : ""}.`;

  const parts: (HTMLElement | null)[] = [
    el("h2", {}, d.vendor ?? "No vendor named"),
    el("div", { class: "amount" }, amountOf(d)),
    el("p", { class: "small" }, [d.file, d.invoice_number, d.date && `dated ${d.date}`, d.due_date && `due ${d.due_date}`, d.payment_terms].filter(Boolean).join(" · ")),
    d.resolved ? resolution(d.resolved) : null,
    el("h3", {}, d.resolved ? "What the checks found" : "Why it is held"),
    held.length ? flagList(held) : el("p", { class: "small" }, "Nothing that holds, warns or blocks."),
    advice ? el("h3", {}, d.resolved ? "The reason on record" : "Advice (a recommendation, not a decision)") : null, advice,
    el("h3", {}, "Line items"), el("div", { class: "scroll" }, lines),
    d.notes ? el("p", { class: "small" }, `Vendor's notes: ${d.notes}`) : null,
    notes.length ? el("details", {}, el("summary", {}, `Other notes on this invoice (${notes.length})`), flagList(notes)) : null,
    el("details", {}, el("summary", {}, "Steps taken so far"), el("pre", {}, d.trail.map((t) => JSON.stringify(t)).join("\n"))),
    d.resolved ? null : decisionForm(d, pays)];
  $("detail").replaceChildren(...parts.filter((p): p is HTMLElement => p !== null));
  // On a narrow screen the invoice sits below the lists: bring it into view when one is chosen.
  if (window.matchMedia("(max-width: 760px)").matches && (selected !== null || selectedRun !== null) && shownOnce) $("detail").scrollIntoView();
  shownOnce = true;
}

/** How a finished run ended and who resolved it, in words. */
function resolution(r: Resolved): HTMLElement {
  const amount = money(r.amount, r.currency ?? "USD");
  const what = r.status === "PAID" ? `Paid ${amount}.`
    : r.status === "REJECTED" ? "Rejected. Nothing was paid."
    : r.status.startsWith("SKIPPED") ? "Skipped. Nothing was decided or paid for this file."
    : r.status === "ERROR" ? "The file could not be read."
    : r.status === "PAYMENT_UNCONFIRMED" ? `A payment of ${amount} was sent but did not confirm: check with the bank.`
    : `${OUTCOME[r.status] ?? r.status}.`;
  const who = r.by === "person" ? `Resolved by ${r.reviewer ?? "a reviewer"}.`
    : r.by === "none" ? "" : `Resolved by ${r.who.charAt(0).toLowerCase()}${r.who.slice(1)}${r.rule ? ` (rule: ${words(r.rule).toLowerCase()})` : ""}. No person was involved.`;
  return el("div", { class: `notice ${r.status === "PAID" ? "done" : r.status.startsWith("SKIPPED") ? "" : "error"}` },
    el("strong", {}, what), ` ${who}`, r.note || r.error ? el("div", {}, r.note ?? r.error ?? "") : null);
}

/** For a decision that was recorded but never carried out: one button, which carries it out as recorded. */
function finishForm(d: Detail, stuck: { action: string; reviewer: string; note?: string | null }): HTMLElement {
  const button = el("button", { type: "button", class: stuck.action === "approve" ? "approve" : "reject" },
    stuck.action === "approve" ? "Finish: approve and pay" : "Finish: reject") as HTMLButtonElement;
  const message = el("p", { class: "notice error", role: "status" },
    `${stuck.reviewer} decided to ${stuck.action} this invoice${stuck.note ? ` (“${stuck.note}”)` : ""}, but the decision was not carried out. `
    + "Finishing it carries out that decision, as recorded.");
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const outcome = await api<Outcome>(`/api/queue/${d.id}/decision`, { action: stuck.action, reviewer: stuck.reviewer, note: stuck.note ?? null });
      showOutcome(...describe(outcome));
      selected = null;
      await loadQueue();
    } catch (e) {
      message.textContent = (e as Error).message;
      button.disabled = false;
    }
  });
  return el("form", {}, message, el("div", { class: "buttons" }, button));
}

function decisionForm(d: Detail, pays: string): HTMLElement {
  if (d.stuck) return finishForm(d, d.stuck);
  if (d.id === null) return el("p", {});
  let remembered = "";
  try { remembered = localStorage.getItem("reviewer") ?? ""; } catch { /* storage unavailable: start empty */ }
  const name = el("input", { name: "reviewer", required: "", autocomplete: "name", value: remembered }) as HTMLInputElement;
  const note = el("textarea", { name: "note", rows: "2" }) as HTMLTextAreaElement;
  const approve = el("button", { type: "button", class: "approve" }, "Approve and pay") as HTMLButtonElement;
  const reject = el("button", { type: "button", class: "reject" }, "Reject") as HTMLButtonElement;
  const message = el("p", { class: "notice", role: "status" }, d.cannot_approve ? `This invoice can't be approved: ${d.cannot_approve}.` : pays);
  approve.disabled = d.cannot_approve !== null;

  async function decide(action: "approve" | "reject"): Promise<void> {
    if (!name.value.trim()) { message.className = "notice error"; message.textContent = "Enter your name: the decision is recorded against it."; name.focus(); return; }
    if (action === "reject" && !note.value.trim()) { message.className = "notice error"; message.textContent = "A rejection needs a note: it is the reason that goes back to the vendor."; note.focus(); return; }
    approve.disabled = reject.disabled = true;
    try {
      try { localStorage.setItem("reviewer", name.value.trim()); } catch { /* not remembered */ }
      const outcome = await api<Outcome>(`/api/queue/${d.id}/decision`, { action, reviewer: name.value, note: note.value || null });
      showOutcome(...describe(outcome));
      selected = null;
      await loadQueue();
    } catch (e) {
      message.className = "notice error";
      message.textContent = (e as Error).message;
      approve.disabled = d.cannot_approve !== null;
      reject.disabled = false;
    }
  }
  approve.addEventListener("click", () => decide("approve"));
  reject.addEventListener("click", () => decide("reject"));

  const form = el("form", {}, message,
    el("label", {}, "Your name (recorded with the decision)", name),
    el("label", {}, "Note (required to reject; it is the reason sent back to the vendor)", note),
    el("div", { class: "buttons" }, approve, reject));
  form.addEventListener("submit", (e) => e.preventDefault());
  return form;
}

loadQueue();
