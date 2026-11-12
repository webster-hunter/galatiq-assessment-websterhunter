// The review screen. Plain TypeScript, no framework: it lists the invoices held for a person,
// shows one in full, and sends the decision to the API (web/app.py).
//
// Invoice text is written by the vendor. Everything is put on the page with textContent, never
// as HTML, so nothing a vendor writes can become markup or script.

interface Summary {
  id: number; file: string; vendor: string | null; total: string | null; currency: string;
  usd_estimate: string | null; reasons: string[]; recommendation: string | null; has_saved_run: boolean;
  stuck?: { action: string; reviewer: string } | false;
}
interface Flag { code: string; severity: string; meaning: string; detail: Record<string, unknown>; }
interface Advice {
  by: string; recommendation: string | null; rationale: string; open_questions: string[]; note: string | null;
  lookups: { tool: string; arguments: unknown; result: unknown }[]; verified: boolean;
}
interface Detail extends Summary {
  status: string; invoice_number: string | null; date: string | null; due_date: string | null;
  payment_terms: string | null; notes: string | null; subtotal: string | null; tax_amount: string | null; shipping: string | null;
  line_items: { item: string | null; quantity: string | null; unit_price: string | null; note: string | null }[];
  flags: Flag[]; pays: { amount: string | null; currency: string; already_paid: string | null };
  cannot_approve: string | null; conversion: { usd_estimate: string; as_of?: string } | null;
  advice: Advice | null; trail: unknown[];
}
interface Decided {
  id: number; file: string; vendor: string | null; total: string | null; status: string;
  action: string | null; reviewer: string | null; note: string | null; amount: string | null;
}
interface Queue { waiting: Summary[]; decided: Decided[]; paid: Record<string, string>; payments: number; }

const $ = (id: string) => document.getElementById(id) as HTMLElement;
let selected: number | null = null;

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
  const text = n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return currency === "USD" ? `$${text}` : `${text} ${currency}`;
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

async function loadQueue(): Promise<void> {
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

  const waiting = queue.waiting.map((s) => {
    const button = el("button", { type: "button", "aria-current": String(s.id === selected) },
      el("div", { class: "row" }, el("strong", {}, s.vendor ?? "No vendor named"), el("span", {}, money(s.total, s.currency))),
      el("div", { class: "small" }, s.file),
      el("div", { class: "small" }, s.stuck ? `Decided (${s.stuck.action} by ${s.stuck.reviewer}) but not carried out`
        : s.reasons.map(words).join(", ")));
    button.addEventListener("click", () => select(s.id));
    return el("li", {}, button);
  });
  $("waiting").replaceChildren(...(waiting.length ? waiting : [el("li", { class: "empty" }, "Nothing is waiting.")]));

  const decided = queue.decided.map((d) => el("li", { class: "done" },
    el("div", { class: "row" }, el("strong", {}, d.vendor ?? "No vendor named"),
      el("span", { class: `tag ${d.action ?? ""}` }, d.action === "approve" ? "Approved" : d.action === "reject" ? "Rejected" : d.status)),
    el("div", { class: "small" }, `${d.file} · ${d.reviewer ?? "nobody"}${d.amount ? ` · paid ${money(d.amount)}` : ""}`),
    d.note ? el("div", { class: "small" }, `“${d.note}”`) : null));
  $("decided").replaceChildren(...(decided.length ? decided : [el("li", { class: "empty" }, "No decisions yet.")]));

  if (selected === null && queue.waiting.length) await select(queue.waiting[0].id);
  if (selected === null) $("detail").replaceChildren(el("p", { class: "empty" }, "Select an invoice to review it."));
}

async function select(id: number): Promise<void> {
  selected = id;
  document.querySelectorAll("#waiting button").forEach((b) => b.setAttribute("aria-current", "false"));
  let d: Detail;
  try {
    d = await api<Detail>(`/api/queue/${id}`);
  } catch (e) {
    $("detail").replaceChildren(el("p", { class: "notice error" }, (e as Error).message));
    return;
  }
  render(d);
  await loadQueue();
}

function render(d: Detail): void {
  const held = d.flags.filter((f) => f.severity !== "INFO");
  const notes = d.flags.filter((f) => f.severity === "INFO");
  const flagList = (flags: Flag[]) => el("ul", { class: "flags" }, ...flags.map((f) => el("li", {},
    el("span", { class: `tag ${f.severity}` }, f.severity === "REVIEW" ? "Held" : f.severity === "WARN" ? "Warning" : f.severity === "BLOCK" ? "Blocks" : "Note"),
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

  const pays = d.pays.already_paid !== null
    ? `Approving pays ${money(d.pays.amount, d.pays.currency)}: the difference. ${money(d.pays.already_paid, d.pays.currency)} was already paid on the earlier version.`
    : `Approving pays ${money(d.pays.amount, d.pays.currency)}${d.conversion ? `, about ${money(d.conversion.usd_estimate)} at the rate service's rate` : ""}.`;

  const parts: (HTMLElement | null)[] = [
    el("h2", {}, d.vendor ?? "No vendor named"),
    el("div", { class: "amount" }, amountOf(d)),
    el("p", { class: "small" }, [d.file, d.invoice_number, d.date && `dated ${d.date}`, d.due_date && `due ${d.due_date}`, d.payment_terms].filter(Boolean).join(" · ")),
    el("h3", {}, "Why it is held"), flagList(held),
    advice ? el("h3", {}, "Advice (a recommendation, not a decision)") : null, advice,
    el("h3", {}, "Line items"), el("div", { class: "scroll" }, lines),
    d.notes ? el("p", { class: "small" }, `Vendor's notes: ${d.notes}`) : null,
    notes.length ? el("details", {}, el("summary", {}, `Other notes on this invoice (${notes.length})`), flagList(notes)) : null,
    el("details", {}, el("summary", {}, "Steps taken so far"), el("pre", {}, d.trail.map((t) => JSON.stringify(t)).join("\n"))),
    decisionForm(d, pays)];
  $("detail").replaceChildren(...parts.filter((p): p is HTMLElement => p !== null));
}

function decisionForm(d: Detail, pays: string): HTMLElement {
  if (d.status !== "WAITING" && !d.stuck) return el("p", { class: "notice done" }, `This invoice has been decided (${d.status.toLowerCase()}).`);
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
      await api(`/api/queue/${d.id}/decision`, { action, reviewer: name.value, note: note.value || null });
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
