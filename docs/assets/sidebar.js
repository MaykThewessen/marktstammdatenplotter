// minimal sidebar interactivity — no build step
(function () {
  const toggle = document.querySelector('.menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => sidebar.classList.toggle('open'));
  }

  // search filter for sidebar nav
  const search = document.querySelector('#sidebar-search');
  if (search) {
    search.addEventListener('input', (e) => {
      const q = e.target.value.trim().toLowerCase();
      document.querySelectorAll('.sidebar-nav a').forEach((a) => {
        const li = a.parentElement;
        if (!q || a.textContent.toLowerCase().includes(q)) {
          li.style.display = '';
        } else {
          li.style.display = 'none';
        }
      });
    });
  }

  // scroll-spy: highlight nav item for current section
  const sections = Array.from(document.querySelectorAll('main section[id]'));
  const navLinks = Array.from(document.querySelectorAll('.sidebar-nav a[href^="#"]'));
  function spy() {
    let active = sections[0];
    const y = window.scrollY + 120;
    for (const s of sections) {
      if (s.offsetTop <= y) active = s;
    }
    navLinks.forEach((a) => {
      a.classList.toggle('active', active && a.getAttribute('href') === '#' + active.id);
    });
  }
  if (sections.length) {
    window.addEventListener('scroll', spy, { passive: true });
    spy();
  }
})();
