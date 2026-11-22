const parcel = document.querySelector('#parcel');
const order = document.querySelector('#job-form');
const note = document.querySelector('#studio-feedback');
function target(sample, key) {
  const url = new URL(location.href);
  url.searchParams.set('sample', sample);
  if (key) url.searchParams.set('key', key);
  else url.searchParams.delete('key');
  return url;
}
parcel?.addEventListener('change', () => location.assign(target(parcel.value)));
document.querySelector('#print-receipt')?.addEventListener('click', () => window.print());
order?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = order.querySelector('[type=submit]');
  button.disabled = true;
  note.hidden = false;
  note.textContent = '正在封存秤单';
  try {
    const response = await fetch('/api/books', {method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({sample: parcel.value})});
    const saved = await response.json();
    if (!response.ok) throw new Error(saved.message);
    const next = target(parcel.value, saved.key);
    if (next.href === location.href) note.textContent = '同一收据留档已复用';
    else location.assign(next);
  } catch (error) {
    note.textContent = error.message || '封存失败，请重试';
  } finally {
    button.disabled = false;
  }
});
const pots = document.querySelector('#pot-filter');
pots?.addEventListener('change', () => {
  for (const row of document.querySelectorAll('#pot-lines tr')) {
    row.hidden = pots.value !== 'all' && row.dataset.pot !== pots.value;
  }
});

// 皮重更正：按容器联动版本，提交后跳转到新签发的更正收据。
const correction = document.querySelector('#correction-form');
if (correction) {
  const choices = JSON.parse(document.querySelector('#correction-choices').textContent);
  const container = document.querySelector('#correction-container');
  const version = document.querySelector('#correction-version');
  const feedback = document.querySelector('#correction-feedback');
  const fill = () => {
    version.replaceChildren(...(choices[container.value] || []).map(v => new Option(v, v)));
  };
  container.addEventListener('change', fill);
  fill();
  correction.addEventListener('submit', async event => {
    event.preventDefault();
    const button = correction.querySelector('[type=submit]');
    button.disabled = true;
    feedback.hidden = false;
    feedback.textContent = '正在按更正皮重重算';
    const body = {tare: [{container: container.value, version: version.value,
      mass: document.querySelector('#correction-mass').value,
      unit: document.querySelector('#correction-unit').value}],
      reason: document.querySelector('#correction-reason').value};
    try {
      const response = await fetch(`/api/books/${correction.dataset.key}/corrections`,
        {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.message);
      if (saved.reused) {
        feedback.textContent = '同一更正已签发过，直接跳到该版';
      }
      const url = new URL(location.href);
      url.searchParams.set('key', saved.key);
      url.pathname = url.pathname.replace(/\/studio\/[^/]+$/, '/studio/receipt');
      location.assign(url);
    } catch (error) {
      feedback.textContent = error.message || '更正失败，旧收据仍有效';
      button.disabled = false;
    }
  });
}
