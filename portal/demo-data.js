/*
 * Demo mode for the INJINN portal.
 *
 * The project's Firebase backend has been shut down. This script intercepts
 * the portal's requests to Firebase and answers them from a simulated fleet
 * of three vehicles, so the portal can be viewed without a backend.
 *
 * The simulation follows the real system's rules: the same incident types,
 * penalties, states, shift summaries and owner ping.
 *
 * To run the portal against a live Firebase database, set DEMO_MODE = false
 * (or remove this script from index.html).
 */
(function () {
  const DEMO_MODE = true;
  if (!DEMO_MODE) return;

  // Incident types and penalties, as produced by raspberry-pi/main.py
  const INCIDENTS = [
    { event: "Microsleep Detected", penalty: 10, state: "drowsy", weight: 2 },
    { event: "Severe Yawning", penalty: 2, state: "drowsy", weight: 3 },
    { event: "Distraction (>3s)", penalty: 3, state: "distracted", weight: 4 },
    { event: "Speeding Violation", penalty: 1, state: "speeding", weight: 2 },
  ];

  const pad = (n) => String(n).padStart(2, "0");
  function stamp(d) {
    let h = d.getHours() % 12 || 12;
    return {
      time: `${pad(h)}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${d.getHours() < 12 ? "AM" : "PM"}`,
      date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    };
  }
  const session = () => (new Date().getHours() >= 6 && new Date().getHours() < 18 ? "Day" : "Night");

  let pushId = 0;
  const nextKey = () => `-demo${String(++pushId).padStart(6, "0")}`;

  // Past shift summaries give the leaderboard something to rank.
  function pastShifts(scores) {
    const h = {};
    scores.forEach((s, i) => {
      const d = new Date(Date.now() - (scores.length - i) * 12 * 3600 * 1000);
      h[nextKey()] = { event: `Shift Summary (${i % 2 ? "Night" : "Day"})`, penalty: `Score: ${s}`, ...stamp(d) };
    });
    return h;
  }

  const fleet = {
    bus_1: { live: true, score: 100, history: pastShifts([88, 74, 91, 82]) },
    bus_2: { live: true, score: 100, history: pastShifts([95, 90, 86, 93]) },
    bus_3: { live: false, score: 100, history: pastShifts([61, 48, 72, 67]) },
  };

  // Incidents earlier in the current shift, so the chart and history start populated
  const earlier = {
    bus_1: ["Distraction (>3s)", "Severe Yawning", "Microsleep Detected", "Distraction (>3s)"],
    bus_2: ["Severe Yawning", "Distraction (>3s)"],
    bus_3: ["Microsleep Detected", "Speeding Violation", "Distraction (>3s)", "Microsleep Detected", "Severe Yawning"],
  };
  for (const id in earlier) {
    earlier[id].forEach((name, i) => {
      const inc = INCIDENTS.find((x) => x.event === name);
      fleet[id].score -= inc.penalty;
      const d = new Date(Date.now() - (earlier[id].length - i) * 17 * 60 * 1000);
      fleet[id].history[nextKey()] = { event: name, penalty: `-${inc.penalty}`, ...stamp(d) };
    });
  }
  const status = {};
  for (const id in fleet) {
    status[id] = {
      state: fleet[id].live ? "active" : "offline",
      speed_kmh: 0,
      location: { lat: 23.9481, lng: 90.3792 },
      score: fleet[id].score,
      session: session(),
    };
  }
  const clearAt = {};

  function pickIncident() {
    const total = INCIDENTS.reduce((a, b) => a + b.weight, 0);
    let r = Math.random() * total;
    for (const inc of INCIDENTS) if ((r -= inc.weight) < 0) return inc;
    return INCIDENTS[0];
  }

  // One simulation step per portal poll (about once a second)
  function tick() {
    const now = Date.now();
    for (const id in fleet) {
      const bus = fleet[id], st = status[id];
      if (!bus.live) continue;
      if (clearAt[id] && now > clearAt[id]) { st.state = "active"; clearAt[id] = 0; }
      st.speed_kmh = Math.round(35 + 20 * Math.sin(now / 9000 + id.length) + Math.random() * 6);
      st.session = session();

      // Reset a vehicle that has dropped too low, as a shift change would
      if (bus.score < 45) {
        bus.history[nextKey()] = { event: `Shift Summary (${st.session})`, penalty: `Score: ${bus.score}`, ...stamp(new Date()) };
        for (const k in bus.history) if (!bus.history[k].event.includes("Shift Summary")) delete bus.history[k];
        bus.score = 100;
      }

      if (!clearAt[id] && Math.random() < 0.1) {
        const inc = pickIncident();
        if (inc.state === "speeding") st.speed_kmh = 62 + Math.round(Math.random() * 10);
        bus.score -= inc.penalty;
        st.state = inc.state;
        clearAt[id] = now + 3000;
        bus.history[nextKey()] = { event: inc.event, penalty: `-${inc.penalty}`, ...stamp(new Date()) };
      }
      st.score = Math.max(0, bus.score);
    }
  }

  function snapshot() {
    const out = {};
    for (const id in fleet) {
      out[id] = { current_status: JSON.parse(JSON.stringify(status[id])), history: { ...fleet[id].history } };
    }
    return out;
  }

  // An owner ping makes the vehicle answer with an SOS-style emergency for a few seconds
  function handlePing(id) {
    if (!fleet[id] || !fleet[id].live) return;
    status[id].state = "EMERGENCY";
    clearAt[id] = Date.now() + 4000;
  }

  const realFetch = window.fetch.bind(window);
  const json = (body) => Promise.resolve(new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } }));

  window.fetch = function (url, opts) {
    const u = String(url);
    if (!u.includes("firebaseio.com")) return realFetch(url, opts);
    const method = ((opts && opts.method) || "GET").toUpperCase();
    const m = u.match(/fleet\/([^/]+)\/command\.json/);
    if (m && method === "PUT") { handlePing(m[1]); return json({ trigger: false }); }
    if (/fleet\.json/.test(u)) { tick(); return json(snapshot()); }
    return json(null);
  };

  // Banner so viewers know the data is simulated
  document.addEventListener("DOMContentLoaded", () => {
    const b = document.createElement("div");
    b.textContent = "Demo mode: simulated fleet data (backend retired). Owner: admin / admin · Driver: bus_1 / driver";
    b.style.cssText = "position:fixed;bottom:0;left:0;right:0;z-index:200;background:#1e3a8a;color:#fff;" +
      "font:600 11px Inter,sans-serif;letter-spacing:.04em;text-align:center;padding:6px 12px;";
    document.body.appendChild(b);
  });
})();
