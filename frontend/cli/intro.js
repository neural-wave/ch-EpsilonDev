// Opening intro: halftone discs, flat blocks and a band of SPARQL gather around the centre,
// then slide off to the edges and reveal the console. Inspired by the opening of sib.swiss;
// the artwork is drawn here (canvas dots, CSS shapes), not taken from their site.
//
// Plays once per browser session, about 2.2 s. Click, Enter, Space or Esc skips it.
// ?intro=1 forces it, ?intro=0 skips it, and reduced motion skips it.
(function () {
  const params = new URLSearchParams(location.search);
  const force = params.get("intro");
  let seen = false;
  try { seen = sessionStorage.getItem("qg-intro-seen") === "1"; } catch (e) { /* private mode */ }
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (force === "0" || reduce || (seen && force !== "1")) return;
  try { sessionStorage.setItem("qg-intro-seen", "1"); } catch (e) { /* ignore */ }

  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const small = vw < 700;
  const k = small ? 0.55 : 1; // scale everything down on phones

  const overlay = document.createElement("div");
  overlay.className = "intro";
  overlay.setAttribute("aria-hidden", "true");

  // Halftone disc: dots on a hex grid, big and dense on one side, fading to nothing on the other.
  function disc(radius, color, focusAngle) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size = radius * 2;
    const c = document.createElement("canvas");
    c.width = c.height = Math.round(size * dpr);
    c.style.width = c.style.height = `${size}px`;
    const g = c.getContext("2d");
    g.scale(dpr, dpr);
    g.fillStyle = color;
    const step = Math.max(5, radius / 38);
    const fx = Math.cos(focusAngle) * radius * 0.45;
    const fy = Math.sin(focusAngle) * radius * 0.45;
    for (let row = 0; row * step * 0.866 < size; row++) {
      for (let col = 0; col * step < size; col++) {
        const x = col * step + (row % 2 ? step / 2 : 0);
        const y = row * step * 0.866;
        const dx = x - radius;
        const dy = y - radius;
        if (dx * dx + dy * dy > radius * radius) continue;
        const d = Math.hypot(x - radius - fx, y - radius - fy) / (radius * 1.25);
        const r = step * 0.52 * Math.max(0, 1 - d * d);
        if (r < 0.35) continue;
        g.beginPath();
        g.arc(x, y, r, 0, Math.PI * 2);
        g.fill();
      }
    }
    return c;
  }

  const items = [];
  function place(el, cls, w, h) {
    el.classList.add("intro-item", ...cls.split(" "));
    if (w) el.style.width = `${w}px`;
    if (h) el.style.height = `${h}px`;
    overlay.appendChild(el);
    return el;
  }
  const add = (el, from, to, delay) => items.push({ el, from, to, delay });
  const box = (cls, w, h) => place(document.createElement("div"), cls, w, h);

  // Discs
  add(place(disc(230 * k, "#3368ed", -0.6), "intro-disc"), [0, -20 * k], [-vw * 0.24, -vh * 0.6], 0);
  add(place(disc(280 * k, "#e20613", Math.PI), "intro-disc"), [-420 * k, 20 * k], [-vw * 0.85, 80], 60);
  add(place(disc(250 * k, "#e20613", 0), "intro-disc"), [420 * k, 0], [vw * 0.85, vh * 0.3], 120);

  // Flat blocks
  add(box("intro-blk intro-red", 120 * k, 120 * k), [-110 * k, -150 * k], [-vw * 0.3, -vh * 0.9], 80);
  add(box("intro-blk intro-mint", 130 * k, 130 * k), [210 * k, -70 * k], [vw * 0.7, -vh * 0.7], 140);
  add(box("intro-blk intro-pink", 190 * k, 70 * k), [-40 * k, 150 * k], [-vw * 0.35, vh * 0.9], 100);
  add(box("intro-blk intro-red intro-sm", 50 * k, 50 * k), [150 * k, 170 * k], [vw * 0.45, vh * 0.9], 160);
  add(box("intro-blk intro-pink intro-sm", 70 * k, 70 * k), [-250 * k, 110 * k], [-vw * 0.7, vh * 0.6], 40);

  // Band of SPARQL: two halves that split apart at the end
  const text = "SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code . } } GROUP BY ?sex ";
  const bandWidth = Math.min(vw * 0.82, 1100);
  function bandHalf(side) {
    const half = document.createElement("div");
    half.className = `intro-band intro-band-${side}`;
    half.style.width = `${bandWidth / 2}px`;
    const inner = document.createElement("div");
    inner.style.width = `${bandWidth}px`;
    inner.style[side === "l" ? "marginLeft" : "marginLeft"] = side === "l" ? "0" : `-${bandWidth / 2}px`;
    inner.innerHTML = [0, 1, 2, 3].map((i) => `<span>${text.repeat(2).slice(i * 9, i * 9 + 180)}</span>`).join("");
    half.appendChild(inner);
    half.classList.add("intro-item");
    overlay.appendChild(half);
    return half;
  }
  add(bandHalf("l"), [-bandWidth / 4, 0], [-vw * 0.8, 0], 150);
  add(bandHalf("r"), [bandWidth / 4, 0], [vw * 0.8, 0], 150);

  document.body.appendChild(overlay);

  // ---------- timeline (Web Animations API) ----------
  const ease = "cubic-bezier(0.22, 0.8, 0.2, 1)";
  const move = (it, x, y, s, o) => `translate(-50%, -50%) translate(${x}px, ${y}px) scale(${s})`;
  const animations = [];
  items.forEach((it) => {
    const [fx, fy] = it.from;
    const [tx, ty] = it.to;
    animations.push(it.el.animate([
      { transform: move(it, fx * 1.5, fy * 1.5, 0.7), opacity: 0 },
      { transform: move(it, fx, fy, 1), opacity: 1, offset: 0.34 },
      { transform: move(it, fx, fy, 1), opacity: 1, offset: 0.5 },
      { transform: move(it, tx, ty, 1.1), opacity: 1 },
    ], { duration: 2000, delay: it.delay, easing: ease, fill: "both" }));
  });
  animations.push(overlay.animate([{ opacity: 1 }, { opacity: 1, offset: 0.55 }, { opacity: 0 }],
    { duration: 2150, easing: "ease-in-out", fill: "forwards" }));

  let done = false;
  function finish() {
    if (done) return;
    done = true;
    animations.forEach((a) => a.cancel());
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("pointerdown", finish, true);
    document.documentElement.classList.remove("intro-playing");
  }
  function onKey(e) { if (["Escape", "Enter", " "].includes(e.key)) finish(); }
  document.documentElement.classList.add("intro-playing");
  document.addEventListener("keydown", onKey);
  window.addEventListener("pointerdown", finish, true);
  animations[animations.length - 1].onfinish = finish;
})();
