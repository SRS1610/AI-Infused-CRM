(function(){
  var endpoint = __ENDPOINT__;
  document.querySelectorAll('[data-tessera-form]').forEach(function (host) {
    var f = document.createElement('form');
    f.innerHTML =
      '<label>Name<input name="name" required></label>' +
      '<label>Email<input name="email" type="email" required></label>' +
      '<label>Company<input name="company"></label>' +
      '<label>Phone<input name="phone"></label>' +
      '<label>Message<textarea name="message"></textarea></label>' +
      '<input name="website_url" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">' +
      '<button type="submit">Send</button><p role="status"></p>';
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var d = {};
      new FormData(f).forEach(function (v, k) { d[k] = v; });
      d.page = location.href;
      fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d) })
        .then(function (r) {
          f.querySelector('[role=status]').textContent = r.ok ? 'Thank you, we will be in touch.' : 'Sorry, something went wrong.';
          if (r.ok) f.reset();
        });
    });
    host.appendChild(f);
  });
})();
