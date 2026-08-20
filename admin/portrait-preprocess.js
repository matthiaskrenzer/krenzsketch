const summaryEl = document.getElementById("summary");
const variantsEl = document.getElementById("variants");
const lineartEl = document.getElementById("lineart");
const pathsEl = document.getElementById("paths");

async function load() {
	try {
		const res = await fetch("/tmp/portrait-preprocess/preprocess-summary.json", { cache: "no-store" });
		if (!res.ok) throw new Error("preprocess-summary.json fehlt — zuerst lineart.py ausführen");
		const summary = await res.json();
		summaryEl.textContent = JSON.stringify(summary, null, 2);

		for (const name of ["canny", "xdog", "hybrid"]) {
			const v = summary.variants?.[name];
			if (!v) continue;
			const card = document.createElement("article");
			card.className = "card" + (summary.chosenVariant === name ? " chosen" : "");
			const ev = v.evaluation || {};
			card.innerHTML = `
				<h3>${name}${summary.chosenVariant === name ? " ★" : ""}</h3>
				<img src="/tmp/portrait-preprocess/portrait-lineart-${name}.png" alt="${name}" />
				<p class="muted">Pfade: ${ev.n_paths ?? "?"} · Score: ${ev.score ?? "?"}</p>
				<p class="muted">${JSON.stringify(ev.counts || {})}</p>
			`;
			variantsEl.appendChild(card);
		}

		lineartEl.src = "/tmp/portrait-preprocess/portrait-lineart-source.png";
		pathsEl.data = "/tmp/portrait-preprocess/portrait-path-preview.svg";
	} catch (err) {
		summaryEl.textContent = String(err);
	}
}

load();
