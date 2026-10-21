// Runs before first paint (loaded synchronously from <head>) so there is no light/dark flash.
(function () {
  var t = null;
  try { t = localStorage.getItem('eitri-theme'); } catch (e) { /* storage blocked */ }
  if (t !== 'light' && t !== 'dark') t = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  var m = document.querySelector('meta[name="theme-color"]');
  if (m) m.setAttribute('content', t === 'light' ? '#efe9de' : '#0c0a09');
})();
