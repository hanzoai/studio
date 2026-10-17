/* gui's theme class follows next-themes' (see @hanzo/build index.html). A file,
   not an inline script, so the page's policy allows scripts from itself only;
   loaded in <head> before the bundle, so the first paint is already themed. */
;(function () {
  var e = document.documentElement, was
  function s() {
    var l = e.classList.contains('light')
    if (l === was) return
    was = l
    e.classList.toggle('t_light', l)
    e.classList.toggle('t_dark', !l)
  }
  s()
  new MutationObserver(s).observe(e, { attributes: true, attributeFilter: ['class'] })
})()
