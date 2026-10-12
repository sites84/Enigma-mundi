(() => {
  const grid = document.querySelector("#post-grid");
  const status = document.querySelector("#status-message");
  const year = document.querySelector("#year");
  const today = document.querySelector("#today-label");
  const searchInput = document.querySelector("#post-search");
  const categorySelect = document.querySelector("#category-filter");
  const moreButton = document.querySelector("#load-more");
  const pageSize = 18;
  let allPosts = [];
  let visibleCount = pageSize;

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

  const postCategory = post => String(post.category_label || post.category || "Investigação").trim();
  const postDate = post => post.published_at || post.date || "";

  function renderPost(post) {
    const title = escapeHtml(post.title || "Matéria em preparação");
    const excerpt = escapeHtml(post.excerpt || post.description || "Consulte a publicação original para acessar o conteúdo completo.");
    const category = escapeHtml(postCategory(post));
    const dateValue = postDate(post);
    const date = escapeHtml(dateLabel(dateValue));
    const source = escapeHtml(post.source_name || "Fonte externa");
    const image = safeUrl(post.image_url || post.cover_image);
    const link = safeUrl(post.source_url || post.url || post.path);
    const imageCredit = escapeHtml(post.image_credit || "");
    const imageLicense = escapeHtml(post.image_license || "");
    const imageSource = safeUrl(post.image_source_url);
    const attribution = imageCredit || imageLicense || imageSource
      ? `<div class="image-attribution">Imagem: ${imageSource ? `<a href="${escapeHtml(imageSource)}" target="_blank" rel="noopener noreferrer">${imageCredit || "ver origem"}</a>` : (imageCredit || "crédito não informado")}${imageLicense ? ` · Licença: ${imageLicense}` : ""}</div>`
      : "";
    const imageMarkup = image
      ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(post.image_alt || post.title || "Imagem da publicação")}" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.remove();this.parentElement.classList.add('image-placeholder');this.parentElement.textContent='◉'">`
      : '<div class="image-placeholder" aria-hidden="true">◉</div>';
    const titleMarkup = link
      ? `<a href="${escapeHtml(link)}" class="post-title" target="_blank" rel="noopener noreferrer">${title}</a>`
      : `<h3 class="post-title">${title}</h3>`;
    const readMarkup = link
      ? `<a class="read-more" href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">Ler na fonte original <span aria-hidden="true">↗</span></a>`
      : '<span class="read-more">Em preparação</span>';
    return `<article class="post-card"><div class="post-image">${imageMarkup}<span class="post-category">${category}</span></div>${attribution}<div class="post-body"><div class="post-meta"><span>${source}</span><time datetime="${escapeHtml(dateValue)}">${date}</time></div>${titleMarkup}<p class="post-excerpt">${excerpt}</p>${readMarkup}</div></article>`;
  }

  function populateCategories() {
    if (!categorySelect) return;
    const categories = [...new Set(allPosts.map(postCategory).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt-BR"));
    categorySelect.innerHTML = '<option value="">Todas as categorias</option>' +
      categories.map(category => `<option value="${escapeHtml(category.toLocaleLowerCase("pt-BR"))}">${escapeHtml(category)}</option>`).join("");
  }

  function renderFilteredPosts(reset = false) {
    if (reset) visibleCount = pageSize;
    const term = (searchInput?.value || "").trim().toLocaleLowerCase("pt-BR");
    const category = (categorySelect?.value || "").toLocaleLowerCase("pt-BR");
    const filtered = allPosts.filter(post => {
      const searchable = [post.title, post.excerpt, post.description, post.source_name, postCategory(post)]
        .filter(Boolean).join(" ").toLocaleLowerCase("pt-BR");
      return (!term || searchable.includes(term)) &&
        (!category || postCategory(post).toLocaleLowerCase("pt-BR") === category);
    });
    const page = filtered.slice(0, visibleCount);
    grid.innerHTML = page.length
      ? page.map(renderPost).join("")
      : '<div class="empty-state"><strong>Nenhum resultado encontrado.</strong><br>Tente outros termos ou selecione outra categoria.</div>';
    if (status) status.textContent = filtered.length
      ? `Exibindo ${page.length} de ${filtered.length} ${filtered.length === 1 ? "publicação" : "publicações"}`
      : "Nenhuma publicação corresponde aos filtros.";
    if (moreButton) {
      moreButton.hidden = page.length >= filtered.length;
      moreButton.textContent = "Carregar mais matérias";
    }
  }

  async function loadPosts() {
    try {
      const response = await fetch("data/posts.json", { cache: "no-store" });
      if (!response.ok) throw new Error("posts.json indisponível");
      const payload = await response.json();
      allPosts = (Array.isArray(payload) ? payload : payload.posts || [])
        .filter(post => post && post.status !== "draft")
        .sort((a, b) => new Date(postDate(b) || 0) - new Date(postDate(a) || 0));
      if (!allPosts.length) {
        status.textContent = "";
        grid.innerHTML = '<div class="empty-state"><strong>O arquivo está sendo preparado.</strong><br>As primeiras publicações aparecerão aqui após a validação das fontes.</div>';
        if (moreButton) moreButton.hidden = true;
        return;
      }
      populateCategories();
      renderFilteredPosts(true);
    } catch (error) {
      console.error("Não foi possível carregar as publicações:", error);
      status.textContent = "";
      grid.innerHTML = '<div class="empty-state">Não foi possível carregar as matérias agora. Tente novamente mais tarde.</div>';
      if (moreButton) moreButton.hidden = true;
    }
  }

  searchInput?.addEventListener("input", () => renderFilteredPosts(true));
  categorySelect?.addEventListener("change", () => renderFilteredPosts(true));
  moreButton?.addEventListener("click", () => {
    visibleCount += pageSize;
    renderFilteredPosts(false);
  });
  loadPosts();
})();