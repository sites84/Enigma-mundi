(() => {
  const grid = document.querySelector("#post-grid");
  const status = document.querySelector("#status-message");
  const year = document.querySelector("#year");
  const today = document.querySelector("#today-label");
  if (year) year.textContent = String(new Date().getFullYear());
  if (today) today.textContent = new Intl.DateTimeFormat("pt-BR", { month: "long", year: "numeric" }).format(new Date()).toUpperCase();

  const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[ch]);

  const dateLabel = value => {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return "Data não informada";
    return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short", year: "numeric" }).format(date);
  };

  const safeUrl = value => {
    try {
      const url = new URL(value, window.location.href);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch { return ""; }
  };

  function renderPost(post) {
    const title = escapeHtml(post.title || "Matéria em preparação");
    const excerpt = escapeHtml(post.excerpt || post.description || "A reportagem completa será disponibilizada em breve.");
    const category = escapeHtml(post.category_label || post.category || "Investigação");
    const date = escapeHtml(dateLabel(post.published_at || post.date));
    const source = escapeHtml(post.source_name || "Fonte externa");
    const image = safeUrl(post.image_url || post.cover_image);
    const link = safeUrl(post.source_url || post.url || post.path);
    const imageMarkup = image
      ? `<img src="${escapeHtml(image)}" alt="${title}" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove();this.parentElement.classList.add('image-placeholder');this.parentElement.textContent='◉'">`
      : '<div class="image-placeholder" aria-hidden="true">◉</div>';
    const titleMarkup = link
      ? `<a href="${escapeHtml(link)}" class="post-title">${title}</a>`
      : `<h3 class="post-title">${title}</h3>`;
    const readMarkup = link
      ? `<a class="read-more" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">Ler na fonte original <span aria-hidden="true">↗</span></a>`
      : '<span class="read-more">Em preparação</span>';
    return `<article class="post-card"><div class="post-image">${imageMarkup}<span class="post-category">${category}</span></div><div class="post-body"><div class="post-meta"><span>${source}</span><time>${date}</time></div>${titleMarkup}<p class="post-excerpt">${excerpt}</p>${readMarkup}</div></article>`;
  }

  async function loadPosts() {
    try {
      const response = await fetch("data/posts.json", { cache: "no-store" });
      if (!response.ok) throw new Error("posts.json indisponível");
      const payload = await response.json();
      const posts = (Array.isArray(payload) ? payload : payload.posts || [])
        .filter(post => post && post.status !== "draft")
        .sort((a, b) => new Date(b.published_at || b.date || 0) - new Date(a.published_at || a.date || 0));
      if (!posts.length) {
        status.textContent = "";
        grid.innerHTML = '<div class="empty-state"><strong>O arquivo está sendo preparado.</strong><br>As primeiras reportagens aparecerão aqui após a ativação e validação das fontes.</div>';
        return;
      }
      status.textContent = `${posts.length} ${posts.length === 1 ? "item de curadoria" : "itens de curadoria"}`;
      grid.innerHTML = posts.map(renderPost).join("");
    } catch (error) {
      console.error("Não foi possível carregar as publicações:", error);
      status.textContent = "";
      grid.innerHTML = '<div class="empty-state">Não foi possível carregar as matérias agora. Tente novamente mais tarde.</div>';
    }
  }
  loadPosts();
})();