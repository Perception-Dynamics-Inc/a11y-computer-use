// Preserve the frozen Wikipedia DOM while replacing its unavailable online
// ResourceLoader sorter with an offline handler on the real table header.
addEventListener('load', () => {
  const header = [...document.querySelectorAll('th')].find(h => h.textContent.trim() === 'First ascent');
  if (!header) throw Error('Frozen page lost First ascent');
  header.scrollIntoView({block: 'center'});
  let descending = false;
  header.addEventListener('click', () => {
    const body = header.closest('table').tBodies[0];
    const rows = [...body.rows].filter(r => r.querySelector('td'));
    const before = rows.map(r => r.textContent).join('\n');
    const column = header.cellIndex;
    rows.sort((a, b) => a.cells[column].textContent.localeCompare(b.cells[column].textContent) * (descending ? -1 : 1));
    descending = !descending;
    rows.forEach(r => body.append(r));
    if (before !== rows.map(r => r.textContent).join('\n')) document.title = 'Offline sort confirmed';
  });
});
