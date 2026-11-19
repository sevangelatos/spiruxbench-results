(() => {
  // Chart lightbox: charts are 1400px wide, so on phones tapping opens them at full size
  const dlg = document.getElementById('lightbox');
  const big = dlg.querySelector('img');
  document.querySelectorAll('a.zoom').forEach(a => {
    a.addEventListener('click', e => {
      if (!dlg.showModal) return;           // very old browser: fall back to opening the image
      e.preventDefault();
      const img = a.querySelector('img');
      big.src = a.href;
      big.alt = img.alt;
      dlg.showModal();
      dlg.scrollTo(0, 0);
    });
  });
  dlg.addEventListener('click', e => { if (e.target !== big) dlg.close(); });
  dlg.addEventListener('close', () => { big.removeAttribute('src'); });
})();
