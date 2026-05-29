async function refresh() {
  const s = await new Promise((res) => chrome.runtime.sendMessage({ type: "get_status" }, res));
  const dot = document.getElementById("dot");
  dot.className = "dot " + (s.state === "connected" ? "ok" : s.state === "error" ? "err" : "wait");
  document.getElementById("state").textContent = s.state || "(unknown)";
  document.getElementById("ws").textContent = s.workspace || "(none)";
  document.getElementById("svc").textContent = s.service_id || "(not registered)";
  document.getElementById("http").textContent = s.http_base || "(unavailable)";
  document.getElementById("err").textContent = s.error || "none";
}
document.getElementById("copy").onclick = async () => {
  const t = document.getElementById("http").textContent;
  await navigator.clipboard.writeText(t);
};
document.getElementById("reconnect").onclick = async () => {
  await new Promise((res) => chrome.runtime.sendMessage({ type: "reconnect" }, res));
  setTimeout(refresh, 400);
};
document.getElementById("open").onclick = () => {
  chrome.tabs.create({ url: "https://hypha.aicell.io" });
};
refresh();
setInterval(refresh, 1500);
