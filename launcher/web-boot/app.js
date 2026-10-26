(() => {
  const status = document.getElementById("status");
  function showInput() {
    status.textContent = "Showing HDMI 1…";
    try {
      tizen.tvwindow.show(() => {
        status.textContent = "HDMI 1 active";
      }, error => {
        status.textContent = `HDMI 1 selected, but video did not display: ${error.message || error.name}`;
      }, ["0%", "0%", "100%", "100%"], "MAIN", "FRONT");
    } catch (error) {
      status.textContent = `HDMI 1 selected, but video did not display: ${error.message || error.name}`;
    }
  }
  function selectHDMI1() {
    try {
      tizen.systeminfo.getPropertyValue("VIDEOSOURCE", sources => {
        const all = [...(sources.connected || []), ...(sources.disconnected || [])];
        const source = all.find(item => String(item.type).toUpperCase() === "HDMI" && Number(item.number) === 1);
        if (!source) {
          status.textContent = `Could not find HDMI 1. TV reported: ${all.map(item => `${item.type}:${item.number}`).join(", ") || "no inputs"}`;
          return;
        }
        try {
          tizen.tvwindow.setSource(source, showInput, error => {
            status.textContent = `Could not select HDMI 1: ${error.message || error.name}`;
          });
        } catch (error) {
          status.textContent = `Could not select HDMI 1: ${error.message || error.name}`;
        }
      }, error => {
        status.textContent = `Could not read TV inputs: ${error.message || error.name}`;
      });
    } catch (error) {
      status.textContent = `Could not read TV inputs: ${error.message || error.name}`;
    }
  }
  setTimeout(selectHDMI1, 500);
})();
