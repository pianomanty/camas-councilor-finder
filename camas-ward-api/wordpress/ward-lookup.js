/**
 * Camas Ward Lookup — JavaScript
 * Enqueue this as a WordPress script (do NOT paste into Custom HTML block).
 * Calls the local Flask API at /ward-api/ via reverse proxy.
 */
(function () {
  var form = document.getElementById('address-form');
  var input = document.getElementById('address');
  var btn = form ? form.querySelector('button[type="submit"]') : null;
  var result = document.getElementById('results');

  if (!form || !input || !btn || !result) return;

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    var address = input.value.trim();
    if (!address) return;

    btn.disabled = true;
    btn.textContent = 'Looking up…';
    result.style.display = 'none';

    try {
      var resp = await fetch('/ward-api/api/lookup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address: address }),
      });
      var data = await resp.json();

      if (!resp.ok) {
        result.innerHTML = '<p style="color: #c00;">' + escapeHtml(data.error) + '</p>';
      } else {
        var boundaryNote = data.on_boundary
          ? '<p style="color: #b8860b;">⚠ This address is near a ward boundary.</p>'
          : '';
        var wardHtml = data.wards.map(function (w) {
          return '<li><strong>' + escapeHtml(w.ward) + '</strong> — ' + escapeHtml(w.councilors) + '</li>';
        }).join('');
        result.innerHTML =
          '<p><strong>Matched address:</strong> ' + escapeHtml(data.matched_address) + '</p>' +
          boundaryNote +
          '<p><strong>Your councilors:</strong></p><ul>' + wardHtml + '</ul>' +
          '<p><strong>At-large councilor:</strong> ' + escapeHtml(data.at_large) + '</p>';
      }
      result.style.display = 'block';
    } catch (err) {
      result.innerHTML = '<p style="color: #c00;">Network error. Please try again.</p>';
      result.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Find my councilors';
    }
  });

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }
})();
