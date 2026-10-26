(() => {
  const cards = Array.from(document.querySelectorAll("[data-hdmi]"));
  const status = document.getElementById("status");
  const clock = document.getElementById("clock");
  const labels = window.QUIET_HOME_INPUT_LABELS || {};
  let selected = 0;

  function displayName(number) {
    const label = String(labels[number] || "").trim();
    return label ? `HDMI ${number} · ${label}` : `HDMI ${number}`;
  }

  cards.forEach(card => {
    const number = Number(card.dataset.hdmi);
    const name = displayName(number);
    card.querySelector(".card-name").textContent = name;
    card.setAttribute("aria-label", `Switch to ${name}`);
  });

  function setFocus(index) {
    selected = (index + cards.length) % cards.length;
    cards.forEach((card, i) => card.classList.toggle("focused", i === selected));
    status.classList.remove("error");
    status.textContent = displayName(Number(cards[selected].dataset.hdmi));
  }

  function showError(message) {
    status.textContent = message;
    status.classList.add("error");
  }

  function inputSource(number, callback, onError) {
    try {
      tizen.systeminfo.getPropertyValue("VIDEOSOURCE", sources => {
        const all = [...(sources.connected || []), ...(sources.disconnected || [])];
        const match = all.find(source => String(source.type).toUpperCase() === "HDMI" && Number(source.number) === number);
        if (match) callback(match);
        else onError(`TV did not report HDMI ${number}. Reported: ${all.map(source => `${source.type}:${source.number}`).join(", ") || "none"}`);
      }, error => onError(`Could not read TV inputs: ${error.message || error.name}`));
    } catch (_) {
      onError(`Could not read TV inputs: ${_.message || _.name || "SystemInfo API unavailable"}`);
    }
  }

  function openInput(number) {
    status.classList.remove("error");
    status.textContent = `Switching to HDMI ${number}…`;
    if (!window.tizen || !tizen.tvwindow) {
      showError("HDMI switching is unavailable.");
      return;
    }
    inputSource(number, source => {
      try {
        tizen.tvwindow.setSource(source, () => {
          status.textContent = `Showing HDMI ${number}…`;
          try {
            tizen.tvwindow.show(() => {
              status.textContent = displayName(number);
            }, error => showError(`HDMI ${number} selected, but video did not display: ${error.message || error.name}`),
            ["0%", "0%", "100%", "100%"], "MAIN", "FRONT");
          } catch (error) {
            showError(`HDMI ${number} selected, but video did not display: ${error.message || error.name}`);
          }
        }, error => showError(`Could not open HDMI ${number}: ${error.message || error.name}`));
      } catch (error) {
        showError(`Could not open HDMI ${number}: ${error.message || error.name}`);
      }
    }, error => showError(error));
  }

  cards.forEach((card, index) => {
    card.addEventListener("focus", () => setFocus(index));
    card.addEventListener("click", () => openInput(Number(card.dataset.hdmi)));
  });
  document.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", " "].includes(event.key) || event.keyCode === 10009) event.preventDefault();
    if (event.key === "ArrowRight" || event.key === "ArrowDown") setFocus(selected + 1);
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") setFocus(selected - 1);
    else if (event.key === "Enter" || event.key === " ") openInput(Number(cards[selected].dataset.hdmi));
    else if (event.keyCode === 10009 || event.key === "Escape" || event.key === "Backspace") {
      try { tizen.application.getCurrentApplication().exit(); } catch (_) { /* no-op */ }
    }
  });
  function updateClock() {
    clock.textContent = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date());
  }
  updateClock(); setInterval(updateClock, 30000); setFocus(0);
})();
