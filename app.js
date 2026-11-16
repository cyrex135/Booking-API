// Same-origin UI bridge keeps the clinic API key on the server.
const slotsEl = document.querySelector('#slots');
const dayEl = document.querySelector('#day');
const selectedEl = document.querySelector('#selected');
const submitEl = document.querySelector('#submit');
const msgEl = document.querySelector('#message');
let chosen = null;
let allSlots = [];
const localDate = value => {
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
};
const formatDateTime = value => new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'short' }).format(new Date(value));

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function renderSlots() {
  chosen = null; submitEl.disabled = true; selectedEl.textContent = 'Select an available time to continue.';
  const matches = allSlots.filter(slot => slot.available && new Date(slot.start) > new Date() && localDate(slot.start) === dayEl.value);
  if (!matches.length) { slotsEl.innerHTML = '<p class="muted">No available times on this date. Try another day.</p>'; return; }
  slotsEl.replaceChildren(...matches.map(slot => {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'slot';
    button.textContent = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(slot.start));
    button.title = slot.provider;
    button.addEventListener('click', () => {
      document.querySelectorAll('.slot').forEach(item => item.classList.remove('selected'));
      button.classList.add('selected'); chosen = slot;
      selectedEl.textContent = `Selected: ${formatDateTime(slot.start)} · ${slot.provider}`;
      submitEl.disabled = false;
    });
    return button;
  }));
}
async function loadSlots() {
  slotsEl.innerHTML = '<p class="muted">Loading available times…</p>';
  try {
    const data = await api('/ui-api/slots');
    allSlots = data.slots || [];
    const firstAvailable = allSlots.find(slot => slot.available && new Date(slot.start) > new Date());
    if (firstAvailable && !allSlots.some(slot => localDate(slot.start) === dayEl.value && slot.available && new Date(slot.start) > new Date())) dayEl.value = localDate(firstAvailable.start);
    renderSlots();
  } catch (error) { slotsEl.innerHTML = `<p class="muted">${error.message}. Please try again later.</p>`; }
}
dayEl.addEventListener('change', renderSlots);
document.querySelector('#booking-form').addEventListener('submit', async event => {
  event.preventDefault(); msgEl.textContent = ''; msgEl.classList.remove('ok');
  if (!chosen) return;
  const form = Object.fromEntries(new FormData(event.currentTarget));
  const payload = { slot_id: chosen.id, name: form.name.trim(), email: form.email.trim() };
  submitEl.disabled = true; submitEl.textContent = 'Booking…';
  try {
    const booking = await api('/ui-api/bookings', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(payload) });
    document.querySelector('#confirm-copy').textContent = `${booking.name}, your visit with ${chosen.provider} is scheduled for ${formatDateTime(chosen.start)}.`;
    document.querySelector('#reference').textContent = booking.id;
    document.querySelector('#confirmation').showModal();
    event.currentTarget.reset(); await loadSlots();
  } catch (error) { msgEl.textContent = error.message; await loadSlots(); }
  finally { submitEl.disabled = !chosen; submitEl.innerHTML = 'Confirm appointment <span aria-hidden="true">→</span>'; }
});
document.querySelector('#close-dialog').addEventListener('click', () => document.querySelector('#confirmation').close());
// Start with the local current date; loadSlots advances to the next date with availability.
dayEl.value = localDate(new Date());
loadSlots();
