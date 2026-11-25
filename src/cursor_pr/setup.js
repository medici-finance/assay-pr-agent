const key = location.hash.slice(1);
history.replaceState(null, "", "/");
const form = document.querySelector("#questions"), label = document.querySelector("label");
const message = document.querySelector("#message"), answer = document.querySelector("#answer");
const multi = document.querySelector("#multi"), button = form.querySelector("button");
const choices = document.querySelector("#choices"), upload = document.querySelector("#upload");
const file = document.querySelector("#file");
file.onchange = async () => {
  const chosen = file.files[0];
  if (chosen && chosen.size <= (step.upload ? 900000 : 24000)) (step.multiline ? multi : answer).value = await chosen.text();
  else message.textContent = "This file is too large to import.";
  file.value = "";
};
let step;
async function api(method, data) {
  const res = await fetch("/step", {method, headers: {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store"});
  if (!res.ok) throw Error("Reopen the setup link printed in your terminal.");
  return res;
}
function fail(error) { form.hidden = true; message.textContent = error.message; }
async function load() {
  const view = await (await api("GET")).json();
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
  step = view; label.textContent = view.label; label.htmlFor = view.multiline ? "multi" : "answer";
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
  if (view.label.startsWith("Human requester")) message.textContent = "Only these accounts can trigger a paid review. Use the numeric user IDs from your GitHub or GitLab profiles.";
  if (view.label.startsWith("Responsible human")) message.textContent = "Enter the operator’s forge username and numeric ID, for example alice:12345.";
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
