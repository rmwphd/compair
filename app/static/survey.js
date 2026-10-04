// Participant comparison loop: fetch a pair, answer it, get the next one.
(() => {
  const app = document.getElementById("app");
  const slug = app.dataset.slug;
  const target = Number(app.dataset.target) || null;
  const $ = (id) => document.getElementById(id);
  const api = (path, body) =>
    fetch(`/api/s/${encodeURIComponent(slug)}/${path}`, {
      method: body ? "POST" : "GET",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    }).then(async (r) => {
      if (!r.ok) {
        let msg = `Something went wrong (${r.status}).`;
        try { msg = (await r.json()).detail || msg; } catch (_) {}
        throw new Error(msg);
      }
      return r.json();
    });

  let current = null;
  let shownAt = 0;
  let busy = false;

  function setSide(el, item) {
    el.querySelector(".label").textContent = item.label;
    el.querySelector(".desc").textContent = item.description || "";
    el.querySelector(".desc").hidden = !item.description;
    el.setAttribute("aria-label", item.label);
    el.classList.remove("picked");
  }

  function render(state) {
    const p = state.progress || { answered: 0 };
    if (target) {
      $("bar").style.width = `${Math.min(100, (100 * p.answered) / target)}%`;
      $("plabel").textContent = `${Math.min(p.answered + (state.done ? 0 : 1), target)} of ${target}`;
    } else {
      $("plabel").textContent = p.answered ? `${p.answered} answered` : "";
    }
    if (state.done) {
      app.hidden = true;
      $("done").hidden = false;
      $("done-msg").textContent =
        `You answered ${p.answered} comparison${p.answered === 1 ? "" : "s"}. Your responses have been saved.`;
      return;
    }
    current = state.pair;
    setSide($("left"), current.left);
    setSide($("right"), current.right);
    shownAt = performance.now();
  }

  function fail(err) {
    $("error").hidden = false;
    $("error").textContent = err.message + " Reload the page to continue; your progress is saved.";
  }

  async function answer(outcome) {
    if (busy || !current) return;
    busy = true;
    app.classList.add("busy");
    if (outcome === "left" || outcome === "right") $(outcome).classList.add("picked");
    try {
      const state = await api("answer", {
        comparison_id: current.comparison_id,
        outcome,
        response_ms: Math.round(performance.now() - shownAt),
      });
      render(state);
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
      app.classList.remove("busy");
    }
  }

  $("left").addEventListener("click", () => answer("left"));
  $("right").addEventListener("click", () => answer("right"));
  $("tie")?.addEventListener("click", () => answer("tie"));
  $("skip")?.addEventListener("click", () => answer("skip"));
  $("finish")?.addEventListener("click", async () => {
    try { render(await api("finish", {})); } catch (e) { fail(e); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || app.hidden) return;
    const k = e.key;
    let outcome = null;
    if (k === "ArrowLeft") outcome = "left";
    else if (k === "ArrowRight") outcome = "right";
    else if ((k === "ArrowDown" || k === "=") && $("tie")) outcome = "tie";
    else if ((k === "s" || k === "S") && $("skip")) outcome = "skip";
    if (outcome) {
      e.preventDefault();
      answer(outcome);
    }
  });

  api("next").then(render).catch(fail);
})();
