(function () {
  // La búsqueda se resuelve en el servidor. Este script solo agrega comodidad:
  // aplicar un filtro al cambiarlo (sin JS queda el botón «Aplicar filtros») y el atajo «/» para escribir.
  const form = document.getElementById('search-form');
  if (!form) return;
  document.querySelectorAll('[data-autosubmit]').forEach((control) => {
    control.addEventListener('change', () => form.requestSubmit());
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;
    const input = document.getElementById('search-input');
    if (input) {
      event.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
