const form = document.querySelector("#questions"), label = document.querySelector("#question");
const message = document.querySelector("#message"), answer = document.querySelector("#answer");
const multi = document.querySelector("#multi"), button = form.querySelector("button");
const choices = document.querySelector("#choices"), upload = document.querySelector("#upload");
const file = document.querySelector("#file");
const unlock = document.querySelector("#unlock"), secret = document.querySelector("#secret");
file.onchange = async () => {
  const chosen = file.files[0];
  if (chosen && chosen.size <= (step.upload ? 900000 : 24000)) (step.multiline ? multi : answer).value = await chosen.text();
  else message.textContent = "This file is too large to import.";
  file.value = "";
};
let step;
const quit = document.querySelector("#quit");
let quitting = false;
quit.onclick = async () => {
  quitting = true;
  quit.disabled = true;
  try {
    await api("POST", {id: step.id, action: "quit"});
    document.querySelector("#registration").hidden = true;
    document.querySelector("#external").hidden = true;
    form.hidden = quit.hidden = true;
    answer.value = multi.value = "";
    message.textContent = "Setup closed. You can close this tab.";
  } catch (error) {
    quitting = false;
    quit.disabled = false;
    await load().catch(fail);
  }
};
// The installer console prints a random setup key; it is kept in a session cookie.
async function api(method, data, path = "/step") {
  const res = await fetch(path, {method, headers: {"Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store"});
  if (res.status === 401) throw Error("key");
  if (!res.ok) throw Error("The installer refused the request. Open http://127.0.0.1:8765/ in this browser and try again.");
  return res;
}
function show_unlock(text) {
  form.hidden = true; quit.hidden = true; unlock.hidden = false; message.textContent = text; secret.focus();
}
function fail(error) {
  form.hidden = true;
  if (error.message === "key") return show_unlock("Enter the setup key printed in the installer console.");
  message.textContent = error.message;
}
unlock.addEventListener("submit", async event => {
  event.preventDefault();
  const submit = unlock.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    await api("POST", {key: secret.value.trim()}, "/login");
    secret.value = ""; unlock.hidden = true; await load();
  } catch (error) {
    if (error.message === "key") show_unlock("That key was not accepted. The setup key is printed in the installer console.");
    else { secret.value = ""; fail(error); }
  } finally { submit.disabled = false; }
});
document.querySelector("#reprint").onclick = async () => {
  const res = await fetch("/print-key", {method: "POST", cache: "no-store"});
  message.textContent = res.ok ? "Setup key printed to the installer console. Copy it from there."
    : res.status === 429 ? "The key was printed a moment ago; copy it from the installer console."
    : "The installer refused the request. Open http://127.0.0.1:8765/ in this browser.";
};
async function load() {
  if (quitting) return;
  const view = await (await api("GET")).json();
  if (quitting) return;
  step = view;
  quit.hidden = !["question", "register"].includes(view.status);
  const registration = document.querySelector("#registration"), external = document.querySelector("#external");
  registration.hidden = view.status !== "register";
  external.hidden = !view.link;
  if (view.link) external.href = view.link;
  if (view.status === "register") {
    form.hidden = true;
    registration.action = view.action;
    registration.elements.manifest.value = JSON.stringify(view.manifest);
    message.textContent = "Register the App in GitHub, then return to this tab.";
    setTimeout(() => load().catch(fail), 800);
    return;
  }
  if (["working","starting"].includes(view.status)) {
    form.hidden = true; message.textContent = view.label || "Checking configuration…";
    setTimeout(() => load().catch(fail), 250); return;
  }
  if (view.status !== "question") {
    form.hidden = true; message.textContent = view.status === "complete"
      ? "Configuration saved. You can return to the installer terminal." : view.label;
    return;
  }
  label.textContent = view.label; label.htmlFor = view.multiline ? "multi" : "answer";
  message.textContent = view.messages.join("\n");
  multi.hidden = !view.multiline; answer.hidden = view.multiline; answer.type = view.secret ? "password" : "text";
  const options = view.label.startsWith("Forge (") ? ["github","gitlab"] :
    view.label.startsWith("Review provider (") ? ["cursor","claude","codex"] :
    view.label.startsWith("Authentication method") ? ["login","api-key"] :
    view.label.startsWith("Project visibility") ? ["private","internal","public"] : [];
  const items = view.choices || options.map(value => ({value, label: {github:"GitHub",gitlab:"GitLab",cursor:"Cursor",claude:"Claude",codex:"Codex",login:"Sign in with the CLI", "api-key":"Provide an API key"}[value] || value}));
  choices.replaceChildren(...items.map(item => new Option(item.label, item.value)));
  if (view.label.startsWith("Forge (")) label.textContent = "Where is your repository hosted?";
  if (view.label.startsWith("Review provider (")) label.textContent = "Which CLI should review changes?";
  if (view.label.startsWith("Authentication method")) message.textContent = "For CLI sign-in, follow the provider’s instructions in the attached installer terminal.";
  if (view.label.startsWith("Checkout path")) message.textContent = "Leave this blank to let the sync container maintain a checkout.";
  if (view.label.startsWith("Human requester")) message.textContent = "Only these accounts can trigger a paid review. Enter GitHub or GitLab usernames (or numeric IDs); the installer looks the IDs up on the forge.";
  if (view.label.startsWith("Responsible human")) message.textContent = "Enter the operator’s forge username, or username:numeric ID, for example alice or alice:12345.";
  if (view.label.startsWith("Maximum estimated")) message.textContent = "Reviews whose estimated prompt exceeds this many tokens are skipped as over-budget. Raise it for large pull requests.";
  if (view.label.startsWith("Maximum estimated input tokens per visual")) message.textContent = "A visual review explains the whole change, so it has its own budget. Over it, the comment still maps where the diff is, but nothing explains it.";
  const prior = view.label.match(/\[([^\]]+)\]:?$/)?.[1];
  if (options.includes(prior)) choices.value = prior;
  choices.hidden = !items.length;
  if (items.length) { answer.hidden = true; label.htmlFor = "choices"; }
  upload.hidden = !view.secret && !view.upload;
  form.hidden = false; button.disabled = false; (items.length ? choices : view.multiline ? multi : answer).focus();
}
form.addEventListener("submit", async event => {
  event.preventDefault(); if (button.disabled) return; button.disabled = true;
  const value = (choices.hidden ? step.multiline ? multi : answer : choices).value; answer.value = ""; multi.value = "";
  try { await api("POST",{id:step.id, answer:value}); await load(); } catch (error) { fail(error); }
});
load().catch(fail);
