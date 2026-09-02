/* cu-arena fixture instrumentation. Every task page loads this file, which is
   also inlined into iframe fixtures. It records every click and every input /
   change event so the harness can count misclicks (clicks whose nearest
   element with an id is not one of the task's allowed targets) after a run.
   A click on an element without any id-bearing ancestor is recorded with its
   tag name and always counts as a misclick. */
(function () {
  var root = window;
  root.__cu = root.__cu || { clicks: [], inputs: [] };
  function idOf(el) {
    var t = el && el.closest ? el.closest('[id]') : null;
    return t ? t.id : ((el && el.tagName) || '').toLowerCase();
  }
  document.addEventListener('click', function (e) {
    root.__cu.clicks.push({ target: idOf(e.target), x: e.clientX, y: e.clientY });
  }, true);
  function record(e) {
    var el = e.target;
    var value = el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value;
    root.__cu.inputs.push({ target: idOf(el), value: value });
  }
  document.addEventListener('input', record, true);
  document.addEventListener('change', record, true);
  /* cu-arena reads this to detect "wasted" actions: a stable digest of the
     page state the planner can influence. Clicks are excluded on purpose. */
  root.__cuState = function () {
    var fields = [];
    var els = document.querySelectorAll('input, select, textarea');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      fields.push([el.id || el.name || i, el.type === 'checkbox' || el.type === 'radio' ? el.checked : el.value]);
    }
    var scrolls = [];
    var boxes = document.querySelectorAll('[data-cu-scroll]');
    for (var j = 0; j < boxes.length; j++) scrolls.push(boxes[j].scrollTop);
    return JSON.stringify([document.title, fields, document.body.innerText, window.scrollY, scrolls, location.hash]);
  };
})();
