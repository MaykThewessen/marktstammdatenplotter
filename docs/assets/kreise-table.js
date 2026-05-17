// Sortable, filterable Kreis table. Data lives in kreise.json.
(async function () {
  const mount = document.querySelector('#kreise-table');
  if (!mount) return;

  const res = await fetch('assets/kreise.json');
  if (!res.ok) {
    mount.innerHTML = '<p style="color:#b91c1c">Failed to load kreise.json</p>';
    return;
  }
  const { snapshot, kreise } = await res.json();

  const filter = document.createElement('div');
  filter.style.cssText = 'display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.75rem;align-items:center;';
  filter.innerHTML = `
    <label style="font-size:0.85rem;color:#6c6c6c">Filter:&nbsp;<input id="k-q" type="search" placeholder="name or state…" style="padding:0.3rem 0.5rem;border:1px solid #e1e4e5;border-radius:3px;font-size:0.85rem;width:14rem"></label>
    <label style="font-size:0.85rem;color:#6c6c6c">Limit:&nbsp;<select id="k-n" style="padding:0.3rem 0.4rem;border:1px solid #e1e4e5;border-radius:3px;font-size:0.85rem">
      <option value="20" selected>top 20</option><option value="50">top 50</option><option value="100">top 100</option><option value="0">all (${kreise.length})</option>
    </select></label>
    <span style="font-size:0.8rem;color:#6c6c6c">snapshot ${snapshot}</span>
  `;
  mount.appendChild(filter);

  const wrap = document.createElement('div');
  wrap.style.cssText = 'overflow-x:auto;';
  mount.appendChild(wrap);

  const cols = [
    { key: 'rank',      label: '#',          num: true,  fmt: v => v.toString() },
    { key: 'name',      label: 'Kreis',      num: false, fmt: v => v },
    { key: 'bundesland',label: 'Bundesland', num: false, fmt: v => v },
    { key: 'wind_gw',   label: 'Wind [GW]',  num: true,  fmt: v => v.toFixed(3) },
    { key: 'wind_n',    label: 'Wind #',     num: true,  fmt: v => v ? v.toString() : '' },
    { key: 'pv_gw',     label: 'PV [GW]',    num: true,  fmt: v => v.toFixed(3) },
    { key: 'pv_n',      label: 'PV #',       num: true,  fmt: v => v ? v.toString() : '' },
    { key: 'total_gw',  label: 'Total [GW]', num: true,  fmt: v => v.toFixed(3) },
  ];

  let sortKey = 'total_gw';
  let sortDir = -1; // -1 desc, 1 asc
  let qStr = '';
  let limit = 20;

  function render() {
    let rows = kreise.slice();
    if (qStr) {
      const q = qStr.toLowerCase();
      rows = rows.filter(r => (r.name + ' ' + r.bundesland).toLowerCase().includes(q));
    }
    rows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (typeof av === 'number') return (av - bv) * sortDir;
      return av.localeCompare(bv) * sortDir;
    });
    rows.forEach((r, i) => (r.rank = i + 1));
    if (limit > 0) rows = rows.slice(0, limit);

    const thead = '<thead><tr>' + cols.map(c => {
      const arrow = sortKey === c.key ? (sortDir < 0 ? ' ▼' : ' ▲') : '';
      return `<th data-k="${c.key}" style="cursor:pointer;text-align:${c.num ? 'right' : 'left'};user-select:none;">${c.label}${arrow}</th>`;
    }).join('') + '</tr></thead>';
    const tbody = '<tbody>' + rows.map(r => '<tr>' + cols.map(c => {
      const align = c.num ? 'right' : 'left';
      return `<td style="text-align:${align}">${c.fmt(r[c.key])}</td>`;
    }).join('') + '</tr>').join('') + '</tbody>';
    wrap.innerHTML = `<table>${thead}${tbody}</table>`;
    wrap.querySelectorAll('th').forEach(th => {
      th.addEventListener('click', () => {
        const k = th.dataset.k;
        if (sortKey === k) sortDir = -sortDir;
        else { sortKey = k; sortDir = cols.find(c => c.key === k).num ? -1 : 1; }
        render();
      });
    });
  }

  filter.querySelector('#k-q').addEventListener('input', e => { qStr = e.target.value.trim(); render(); });
  filter.querySelector('#k-n').addEventListener('change', e => { limit = parseInt(e.target.value, 10); render(); });

  render();
})();
