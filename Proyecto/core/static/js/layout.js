(function () {
  const menuBtn = document.querySelector('[data-open-menu]');
  const sidebar = document.querySelector('.sidebar');
  const scrim = document.querySelector('.scrim');
  const profileBtn = document.querySelector('[data-profile]');
  const notesBtn = document.querySelector('[data-notes]');
  const profileMenu = document.querySelector('[data-profile-menu]');
  const notesMenu = document.querySelector('[data-notes-menu]');
  const mobileMenu = window.matchMedia('(max-width: 900px)');

  function setMenuOpen(open) {
    sidebar?.classList.toggle('open', open);
    scrim?.classList.toggle('show', open);
    menuBtn?.setAttribute('aria-expanded', String(open));
  }

  // Estado de los menús de la barra superior: la clase «open» muestra el panel y aria-expanded lo anuncia.
  function syncPopovers() {
    notesBtn?.setAttribute('aria-expanded', String(Boolean(notesMenu?.classList.contains('open'))));
    profileBtn?.setAttribute('aria-expanded', String(Boolean(profileMenu?.classList.contains('open'))));
  }

  function closePopovers() {
    profileMenu?.classList.remove('open');
    notesMenu?.classList.remove('open');
    syncPopovers();
  }

  menuBtn?.addEventListener('click', () => {
    setMenuOpen(!sidebar?.classList.contains('open'));
  });
  scrim?.addEventListener('click', () => setMenuOpen(false));
  sidebar?.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => {
      if (mobileMenu.matches) setMenuOpen(false);
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (sidebar?.classList.contains('open')) setMenuOpen(false);
    const opened = notesMenu?.classList.contains('open') ? notesBtn : profileMenu?.classList.contains('open') ? profileBtn : null;
    if (opened) {
      closePopovers();
      opened.focus();
    }
  });
  mobileMenu.addEventListener('change', (event) => {
    if (!event.matches) setMenuOpen(false);
  });
  profileBtn?.addEventListener('click', () => {
    notesMenu?.classList.remove('open');
    profileMenu?.classList.toggle('open');
    syncPopovers();
  });
  notesBtn?.addEventListener('click', () => {
    profileMenu?.classList.remove('open');
    notesMenu?.classList.toggle('open');
    syncPopovers();
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.popover-anchor')) closePopovers();
  });

  if (window.lucide) window.lucide.createIcons();

  // Potrero seleccionado, compartido entre módulos (calculadoras, decisiones, comparador...). Es solo una preferencia
  // de pantalla: el servidor siempre vuelve a comprobar que el potrero pertenezca a la cuenta.
  const userNode = document.getElementById('agro-user-id');
  const selKey = 'agroasesor.potrero.' + (userNode ? userNode.textContent.trim() : '');
  window.AgroSel = {
    get() {
      try {
        return window.localStorage.getItem(selKey) || '';
      } catch (error) {
        return '';
      }
    },
    set(id) {
      try {
        window.localStorage.setItem(selKey, String(id));
      } catch (error) {
        /* sin almacenamiento */
      }
    },
  };

  // Un formulario que ya se envió no se vuelve a enviar: un doble clic (o un toque repetido en el celular) creaba el
  // registro dos veces y dejaba dos avisos iguales. Si otra función cancela el envío (validación, dibujo en curso),
  // no se bloquea nada.
  const SENDING_RESET_MS = 15000;
  function releaseForm(form) {
    delete form.dataset.sending;
    form.removeAttribute('aria-busy');
    form.querySelectorAll('[data-was-sending]').forEach((control) => {
      control.disabled = false;
      control.removeAttribute('data-was-sending');
    });
  }
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || (form.method || '').toLowerCase() !== 'post') return;
    if (form.hasAttribute('data-allow-resubmit')) return;
    if (form.dataset.sending === '1') {
      event.preventDefault();
      return;
    }
    // Se revisa al terminar el evento: si alguien llamó a preventDefault, el envío no ocurrió.
    window.setTimeout(() => {
      if (event.defaultPrevented) return;
      form.dataset.sending = '1';
      form.setAttribute('aria-busy', 'true');
      form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]').forEach((control) => {
        if (control.disabled) return;
        control.setAttribute('data-was-sending', '1');
        control.disabled = true;
      });
      // Si la navegación falla (sin red), el formulario vuelve a quedar disponible.
      window.setTimeout(() => releaseForm(form), SENDING_RESET_MS);
    }, 0);
  });
  // Al volver con «Atrás» el navegador restaura la página tal como estaba: se libera lo que quedó bloqueado.
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) document.querySelectorAll('form[data-sending]').forEach(releaseForm);
  });

  // Sesión vencida o cookies borradas: si el servidor avisa en una llamada de la página (fetch), se muestra un aviso y se
  // lleva a la persona al login en vez de dejar botones que no hacen nada.
  const fetchOriginal = window.fetch ? window.fetch.bind(window) : null;
  if (fetchOriginal) {
    window.fetch = async function (...args) {
      const respuesta = await fetchOriginal(...args);
      if (
        (respuesta.status === 401 || respuesta.status === 403) &&
        (respuesta.headers.get('content-type') || '').includes('json')
      ) {
        try {
          const datos = await respuesta.clone().json();
          if (datos && datos.codigo === 'sesion_vencida') {
            const aviso = document.createElement('p');
            aviso.className = 'callout session-expired';
            aviso.setAttribute('role', 'alert');
            aviso.textContent = datos.error + ' Te llevamos al inicio de sesión…';
            document.querySelector('.page-container')?.prepend(aviso);
            setTimeout(() => {
              window.location.assign((datos.login_url || '/login/') + '?vencida=1');
            }, 1800);
          }
        } catch (error) {
          /* respuesta sin JSON válido: se deja pasar */
        }
      }
      return respuesta;
    };
  }
})();
