/* Local dashboard tools. User text is inserted with textContent, never HTML. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  let receiptId = null;
  async function api(url, options) {
    const response = await fetch(url, options);
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'No se pudo completar la acción.');
    return value;
  }
  const json = (method, body) => ({method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const status = (id, text) => { byId(id).textContent = text; };
  async function updateDashboard() {
    if (typeof refresh === 'function') await refresh();
    await loadLocations();
  }
  byId('undo-last').addEventListener('click', async event => {
    event.target.disabled = true;
    try {
      const value = await api('/api/undo', {method:'POST'});
      status('tools-status', value.undone ? 'Cambio deshecho. Gasto #' + value.result.expense_id : 'No hay cambios para deshacer.');
      await updateDashboard();
    } catch (error) { status('tools-status', error.message); }
    finally { event.target.disabled = false; }
  });
  byId('receipt-file').addEventListener('change', () => {
    receiptId = null;
    byId('receipt-review').hidden = true;
    byId('receipt-read').disabled = !byId('receipt-file').files.length;
    status('receipt-status', 'Revisá el total antes de confirmar.');
  });
  byId('receipt-read').addEventListener('click', async event => {
    const file = byId('receipt-file').files[0];
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { status('receipt-status', 'La imagen debe pesar menos de 8 MB.'); return; }
    event.target.disabled = true;
    receiptId = null;
    byId('receipt-review').hidden = true;
    status('receipt-status', 'Leyendo ticket…');
    try {
      const value = await api('/api/receipts/preview', {method:'POST', headers:{'Content-Type':file.type}, body:file});
      receiptId = value.preview_id;
      byId('receipt-name').value = value.name;
      byId('receipt-amount').value = value.amount || '';
      byId('receipt-currency').value = value.currency || 'ARS';
      byId('receipt-date').value = value.expense_date;
      byId('receipt-review').hidden = false;
      status('receipt-status', (value.warnings || []).join(' ') || 'Borrador listo. Todavía no se guardó ningún gasto.');
    } catch (error) { status('receipt-status', error.message); }
    finally { event.target.disabled = false; }
  });
  byId('receipt-review').addEventListener('submit', async event => {
    event.preventDefault();
    if (!receiptId) return;
    byId('receipt-confirm').disabled = true;
    try {
      const value = await api('/api/receipts/confirm', json('POST', {
        preview_id:receiptId, name:byId('receipt-name').value,
        amount:byId('receipt-amount').value, currency:byId('receipt-currency').value,
        expense_date:byId('receipt-date').value,
      }));
      receiptId = null;
      byId('receipt-review').hidden = true;
      status('receipt-status', 'Ticket guardado. Gasto #' + value.id + '.');
      await updateDashboard();
    } catch (error) { status('receipt-status', error.message); }
    finally { byId('receipt-confirm').disabled = false; }
  });
  byId('preferences-form').addEventListener('submit', async event => {
    event.preventDefault();
    byId('preferences-save').disabled = true;
    try {
      await api('/api/preferences', json('PATCH', {language:byId('pref-language').value,
        voice_enabled:byId('pref-voice').checked, timezone:byId('pref-timezone').value}));
      status('preferences-status', 'Preferencias del bot guardadas.');
    } catch (error) { status('preferences-status', error.message); }
    finally { byId('preferences-save').disabled = false; }
  });
  async function loadUsers() {
    const {items} = await api('/api/users');
    byId('users-list').replaceChildren();
    for (const user of items) {
      const row = document.createElement('div');
      row.textContent = user.telegram_user_id + ' · ' + (user.role === 'owner' ? 'propietario' : 'usuario');
      if (user.role !== 'owner') {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'fx-btn'; button.textContent = 'Pausar acceso';
        button.addEventListener('click', async () => {
          button.disabled = true;
          try { await api('/api/users/' + user.telegram_user_id, {method:'DELETE'}); await loadUsers(); }
          catch (error) { status('users-status', error.message); button.disabled = false; }
        });
        row.append(' ', button);
      }
      byId('users-list').append(row);
    }
  }
  byId('users-form').addEventListener('submit', async event => {
    event.preventDefault();
    const raw = byId('new-user-id').value;
    const value = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value <= 0) { status('users-status', 'ID inválido.'); return; }
    byId('user-add').disabled = true;
    try { await api('/api/users', json('POST', {telegram_user_id:value})); await loadUsers(); status('users-status', 'Acceso autorizado.'); }
    catch (error) { status('users-status', error.message); }
    finally { byId('user-add').disabled = false; }
  });
  async function loadLocations() {
    const {items} = await api('/api/locations');
    const list = byId('locations-list'); list.replaceChildren();
    const map = byId('locations-map'); map.replaceChildren();
    map.removeAttribute('hidden'); map.style.display = items.length ? 'block' : 'none';
    if (!items.length) { list.textContent = 'Sin ubicaciones. En Telegram: /ubicacion <ID de gasto> y compartí una ubicación.'; return; }
    const ns = 'http://www.w3.org/2000/svg';
    for (let x = 0; x <= 600; x += 100) {
      const line = document.createElementNS(ns, 'line');
      line.setAttribute('x1', x); line.setAttribute('x2', x); line.setAttribute('y1', 0); line.setAttribute('y2', 300); line.setAttribute('stroke', '#334155'); map.append(line);
    }
    for (let y = 0; y <= 300; y += 100) {
      const line = document.createElementNS(ns, 'line');
      line.setAttribute('x1', 0); line.setAttribute('x2', 600); line.setAttribute('y1', y); line.setAttribute('y2', y); line.setAttribute('stroke', '#334155'); map.append(line);
    }
    for (const item of items) {
      const latitude = Number(item.latitude), longitude = Number(item.longitude);
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) continue;
      const point = document.createElementNS(ns, 'circle');
      point.setAttribute('cx', (longitude + 180) / 360 * 600); point.setAttribute('cy', (90 - latitude) / 180 * 300);
      point.setAttribute('r', 4); point.setAttribute('fill', '#34d399');
      const title = document.createElementNS(ns, 'title'); title.textContent = item.name + ': ' + item.latitude + ', ' + item.longitude; point.append(title); map.append(point);
      const row = document.createElement('p'); row.textContent = '#' + item.id + ' · ' + item.name + ' · ' + item.latitude + ', ' + item.longitude;
      const clear = document.createElement('button'); clear.type = 'button'; clear.className = 'fx-btn'; clear.textContent = 'Quitar ubicación';
      clear.addEventListener('click', async () => {
        clear.disabled = true;
        try { await api('/api/expenses/' + item.id + '/location', json('PATCH', {latitude:null, longitude:null})); await loadLocations(); }
        catch (error) { status('tools-status', error.message); clear.disabled = false; }
      }); row.append(' ', clear); list.append(row);
    }
  }
  (async () => {
    try {
      const prefs = await api('/api/preferences');
      byId('pref-language').value = prefs.language; byId('pref-voice').checked = prefs.voice_enabled; byId('pref-timezone').value = prefs.timezone;
      byId('preferences-save').disabled = false;
      byId('users-tools').hidden = prefs.role !== 'owner';
      if (prefs.role === 'owner') await loadUsers();
      await loadLocations();
    } catch (error) { status('preferences-status', error.message); }
  })();
})();
