(function() {
  'use strict';

  var BRIDGE_BASE = (function() {
    var h = window.location.hostname;
    if (h === 'localhost' || h === '127.0.0.1' || /^\d+\.\d+\.\d+\.\d+$/.test(h)) {
      return window.location.protocol + '//' + h + ':8100';
    }
    return window.location.origin + '/rls-bridge';
  })();
  var BRIDGE_SECRET = window._env_ && window._env_.RLS_BRIDGE_SECRET || 'earshot2025';
  var POLL_INTERVAL = 1500;
  var lastUrl = '';

  function getRoleIdFromUrl() {
    var m = window.location.pathname.match(/\/settings\/members\/roles\/([a-f0-9-]+)/i);
    return m ? m[1] : null;
  }

  function getObjectMetadataIdFromUrl() {
    var m = window.location.pathname.match(/\/settings\/members\/roles\/[a-f0-9-]+\/object\/([a-f0-9-]+)/i);
    if (!m) m = window.location.pathname.match(/\/settings\/members\/roles\/[a-f0-9-]+\/objects\/([a-f0-9-]+)/i);
    if (!m) m = window.location.pathname.match(/\/settings\/roles\/[a-f0-9-]+\/object\/([a-f0-9-]+)/i);
    if (!m) m = window.location.pathname.match(/\/settings\/roles\/[a-f0-9-]+\/objects\/([a-f0-9-]+)/i);
    return m ? m[1] : null;
  }

  function showToast(msg) {
    var existing = document.getElementById('rls-toast');
    if (existing) existing.remove();
    var t = document.createElement('div');
    t.id = 'rls-toast';
    t.textContent = msg;
    t.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#1a1a2e;color:#fff;padding:12px 20px;border-radius:8px;font-size:13px;z-index:100000;opacity:0;transform:translateY(10px);transition:all .3s;pointer-events:none;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif';
    document.body.appendChild(t);
    requestAnimationFrame(function() {
      t.style.opacity = '1';
      t.style.transform = 'translateY(0)';
    });
    setTimeout(function() {
      t.style.opacity = '0';
      t.style.transform = 'translateY(10px)';
      setTimeout(function() { t.remove(); }, 300);
    }, 2500);
  }

  function fetchConfig(callback) {
    var url = BRIDGE_BASE + '/api/rls/config?token=' + encodeURIComponent(BRIDGE_SECRET);
    fetch(url, { headers: { 'Content-Type': 'application/json' } })
      .then(function(r) { return r.json(); })
      .then(function(data) { callback(null, data); })
      .catch(function(e) { callback(e); });
  }

  function postMode(roleId, objectMetadataId, mode, callback) {
    var url = BRIDGE_BASE + '/api/rls/config?token=' + encodeURIComponent(BRIDGE_SECRET);
    console.log('[RLS Inject] POST', url, {roleId: roleId, objectMetadataId: objectMetadataId, mode: mode});
    fetch(url, {
      method: 'POST',
      mode: 'cors',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ roleId: roleId, objectMetadataId: objectMetadataId, mode: mode })
    })
      .then(function(r) {
        console.log('[RLS Inject] POST response:', r.status, r.statusText);
        if (!r.ok) {
          return r.text().then(function(t) {
            var msg = 'HTTP ' + r.status;
            try { var j = JSON.parse(t); msg = j.detail || j.message || msg; } catch(e) { msg += ' ' + t.substring(0, 200); }
            throw new Error(msg);
          });
        }
        return r.json();
      })
      .then(function(data) { callback(null, data); })
      .catch(function(e) { console.error('[RLS Inject] POST error:', e); callback(e); });
  }

  function removeRLS() {
    var el = document.getElementById('rls-inline-section');
    if (el) el.remove();
  }

  function buildLoadingSkeleton() {
    var wrapper = document.createElement('div');
    wrapper.id = 'rls-inline-section';
    wrapper.style.cssText = 'width:100%;margin-top:16px;margin-bottom:4px';

    var card = document.createElement('div');
    card.style.cssText = 'background:inherit;border:none;border-radius:0;padding:0;margin:0;box-shadow:none';

    var shimmer = '@keyframes rlsShimmer{0%{background-position:-200px 0}100%{background-position:200px 0}}';
    var style = document.createElement('style');
    style.textContent = shimmer + ' .rls-skeleton{background:linear-gradient(90deg,#f0f0f0 25%,#e0e0e0 50%,#f0f0f0 75%);background-size:400px 100%;animation:rlsShimmer 1.5s infinite;border-radius:4px}';

    var titleBar = document.createElement('div');
    titleBar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:12px';
    var titleSk = document.createElement('div');
    titleSk.className = 'rls-skeleton';
    titleSk.style.cssText = 'width:140px;height:16px';
    var badgeSk = document.createElement('div');
    badgeSk.className = 'rls-skeleton';
    badgeSk.style.cssText = 'width:50px;height:14px;border-radius:12px';
    titleBar.appendChild(titleSk);
    titleBar.appendChild(badgeSk);

    var descSk = document.createElement('div');
    descSk.className = 'rls-skeleton';
    descSk.style.cssText = 'width:100%;height:10px;margin-bottom:12px';

    var selectSk = document.createElement('div');
    selectSk.className = 'rls-skeleton';
    selectSk.style.cssText = 'width:160px;height:32px;border-radius:6px';

    var hintSk = document.createElement('div');
    hintSk.className = 'rls-skeleton';
    hintSk.style.cssText = 'width:200px;height:10px;margin-top:8px';

    wrapper.appendChild(style);
    card.appendChild(titleBar);
    card.appendChild(descSk);
    card.appendChild(selectSk);
    card.appendChild(hintSk);
    wrapper.appendChild(card);
    return wrapper;
  }

  function buildRLSElement(currentMode, roleId, oid) {
    var wrapper = document.createElement('div');
    wrapper.id = 'rls-inline-section';
    wrapper.style.cssText = 'width:100%;margin-top:16px;margin-bottom:4px';

    var card = document.createElement('div');
    card.style.cssText = 'background:inherit;border:none;border-radius:0;padding:0;margin:0;box-shadow:none';

    var titleRow = document.createElement('div');
    titleRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:4px';

    var title = document.createElement('h3');
    title.style.cssText = 'font-size:14px;font-weight:500;color:inherit;margin:0';
    title.textContent = 'Data Access (RLS)';

    var badge = document.createElement('span');
    badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:12px;font-weight:500;background:#383838;color:#9ca3af;border:1px solid #404040';
    badge.textContent = 'Custom';
    titleRow.appendChild(title);
    titleRow.appendChild(badge);
    card.appendChild(titleRow);

    var desc = document.createElement('p');
    desc.style.cssText = 'font-size:12px;color:var(--text-secondary, #8c8c8c);margin:4px 0 12px 0;line-height:1.5';
    desc.textContent = 'Controls read and write scope for this object. Applies to all operations (read, update, delete).';
    card.appendChild(desc);

    var selectRow = document.createElement('div');
    selectRow.style.cssText = 'display:flex;align-items:center;gap:10px';

    var selectLabel = document.createElement('label');
    selectLabel.style.cssText = 'font-size:13px;color:var(--text-secondary, #666);font-weight:500';
    selectLabel.textContent = 'Scope:';
    selectRow.appendChild(selectLabel);

    var select = document.createElement('select');
    var selectCss = 'appearance:none;-webkit-appearance:none;background:#242424 url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'12\' viewBox=\'0 0 24 24\' fill=\'none\' stroke=\'%239ca3af\' stroke-width=\'2\'%3E%3Cpath d=\'M6 9l6 6 6-6\'/%3E%3C/svg%3E") no-repeat right 8px center;border:1px solid #404040;border-radius:6px;padding:6px 28px 6px 10px;font-size:13px;color:#f5f5f5;cursor:pointer;outline:none;min-width:160px;font-family:inherit;transition:border-color .15s,box-shadow .15s';
    select.style.cssText = selectCss;
    var opts = [
      { value: 'all', text: 'All records' },
      { value: 'own', text: 'My records only' },
      { value: 'none', text: 'No access' }
    ];
    for (var i = 0; i < opts.length; i++) {
      var o = document.createElement('option');
      o.value = opts[i].value;
      o.textContent = opts[i].text;
      if (opts[i].value === currentMode) o.selected = true;
      select.appendChild(o);
    }
    select.addEventListener('mouseenter', function() { select.style.borderColor = '#3b82f6'; select.style.boxShadow = '0 0 0 3px rgba(59,130,246,.2)'; });
    select.addEventListener('mouseleave', function() { select.style.borderColor = '#404040'; select.style.boxShadow = 'none'; });
    select.addEventListener('focus', function() { select.style.borderColor = '#3b82f6'; select.style.boxShadow = '0 0 0 3px rgba(59,130,246,.2)'; });
    select.addEventListener('blur', function() { select.style.borderColor = '#404040'; select.style.boxShadow = 'none'; });
    selectRow.appendChild(select);
    card.appendChild(selectRow);

    var hint = document.createElement('p');
    hint.style.cssText = 'font-size:11px;color:var(--text-tertiary, #aaa);margin:8px 0 0 0;line-height:1.4';
    function updateHint() {
      var v = select.value;
      if (v === 'all') hint.textContent = 'Can see and modify all records.';
      else if (v === 'own') hint.textContent = 'Only see/modify records you created.';
      else hint.textContent = 'Cannot read, update, or delete any records.';
    }
    updateHint();
    card.appendChild(hint);

    select.addEventListener('change', function() {
      var newMode = select.value;
      console.log('[RLS Inject] change event fired, newMode:', newMode, 'roleId:', roleId, 'oid:', oid);
      select.disabled = true;
      hint.textContent = 'Saving...';
      postMode(roleId, oid, newMode, function(err, data) {
        select.disabled = false;
        if (err) {
          console.error('[RLS Inject] save error:', err);
          hint.textContent = 'Error: ' + err.message;
          updateHint();
          return;
        }
        console.log('[RLS Inject] save success:', data);
        if (data.ok) {
          var labels = { all: 'All records', own: 'My records only', none: 'No access' };
          hint.textContent = 'Saved: ' + labels[newMode] + '. Effectif imm\u00e9diatement.';
          showToast('Saved — effective immediately.');
          currentConfig = null;
          lastUrl = '';
        } else {
          hint.textContent = 'Error: ' + (data.detail || 'unknown');
          updateHint();
        }
      });
    });

    wrapper.appendChild(card);
    return wrapper;
  }

  function isReadPermissionEnabled() {
    var toggleContainers = document.querySelectorAll('[class*="toggle"], [role="switch"], input[type="checkbox"]');
    var readToggle = null;
    for (var i = 0; i < toggleContainers.length; i++) {
      var container = toggleContainers[i];
      var parent = container.closest('[class*="row"], [class*="item"], [class*="setting"], [class*="permission"], div');
      if (parent) {
        var labelEl = parent.querySelector('span, label, p, div');
        if (labelEl) {
          var text = labelEl.textContent.toLowerCase().trim();
          if (text.indexOf('voir') !== -1 || text === 'see' || text === 'read' || text.indexOf('lecture') !== -1) {
            readToggle = container;
            break;
          }
        }
      }
    }
    if (!readToggle) {
      return true;
    }
    if (readToggle.tagName === 'INPUT') {
      return readToggle.checked;
    }
    var ariaChecked = readToggle.getAttribute('aria-checked');
    if (ariaChecked !== null) {
      return ariaChecked === 'true';
    }
    var dataState = readToggle.getAttribute('data-state');
    if (dataState) {
      return dataState === 'checked' || dataState === 'on';
    }
    var classList = readToggle.className || '';
    if (classList.indexOf('checked') !== -1 || classList.indexOf('on') !== -1 || classList.indexOf('active') !== -1) {
      return true;
    }
    return true;
  }

  function findInsertionPoint() {
    var allH2 = document.querySelectorAll('h2');
    var allH3 = document.querySelectorAll('h3');
    var headings = [];
    var i, el, text;

    for (i = 0; i < allH2.length; i++) {
      el = allH2[i];
      text = el.textContent.trim().toLowerCase();
      headings.push({ el: el, text: text });
    }
    for (i = 0; i < allH3.length; i++) {
      el = allH3[i];
      text = el.textContent.trim().toLowerCase();
      headings.push({ el: el, text: text });
    }

    var fieldPermsEl = null;

    for (i = 0; i < headings.length; i++) {
      text = headings[i].text;
      if (text.indexOf('autorisation') !== -1 && text.indexOf('champ') !== -1) {
        fieldPermsEl = headings[i].el;
        break;
      }
      if (text.indexOf('field') !== -1 && (text.indexOf('permission') !== -1 || text.indexOf('autorisation') !== -1)) {
        fieldPermsEl = headings[i].el;
        break;
      }
    }

    if (!fieldPermsEl) {
      var allEls = document.querySelectorAll('span, div, p');
      for (i = 0; i < allEls.length; i++) {
        el = allEls[i];
        text = (el.textContent || '').trim().toLowerCase();
        if (text.length < 80) {
          if ((text.indexOf('autorisation') !== -1 && text.indexOf('champ') !== -1) ||
              (text.indexOf('field') !== -1 && text.indexOf('permission') !== -1)) {
            fieldPermsEl = el;
            break;
          }
        }
      }
    }

    if (!fieldPermsEl) {
      console.log('[RLS Inject] Field permissions section not found');
      return null;
    }

    var fieldPermsSection = fieldPermsEl.closest('section, [class*="card"], [class*="Card"], [class*="section"], [class*="block"], [class*="container"]');
    if (!fieldPermsSection) {
      fieldPermsSection = fieldPermsEl.parentElement;
    }

    if (!fieldPermsSection || !fieldPermsSection.parentElement) {
      console.log('[RLS Inject] Cannot find parent of field permissions section');
      return null;
    }

    return { parent: fieldPermsSection.parentElement, before: fieldPermsSection };
  }

  var currentConfig = null;
  var loadingShown = false;

  function tryInject() {
    var roleId = getRoleIdFromUrl();
    var oid = getObjectMetadataIdFromUrl();

    if (!roleId || !oid) {
      removeRLS();
      currentConfig = null;
      lastUrl = window.location.href;
      return;
    }

    if (lastUrl === window.location.href && currentConfig) return;
    lastUrl = window.location.href;

    if (!currentConfig && !loadingShown) {
      var point = findInsertionPoint();
      if (point) {
        removeRLS();
        point.parent.insertBefore(buildLoadingSkeleton(), point.before);
        loadingShown = true;
      }
    }

    fetchConfig(function(err, config) {
      loadingShown = false;
      if (err) {
        console.error('[RLS Inject] Config fetch error:', err);
        removeRLS();
        return;
      }
      currentConfig = config;
      var role = null;
      for (var i = 0; i < config.length; i++) {
        if (config[i].roleId === roleId) { role = config[i]; break; }
      }
      if (!role) { removeRLS(); return; }

      var obj = null;
      if (role.objects) {
        for (var j = 0; j < role.objects.length; j++) {
          if (role.objects[j].objectMetadataId === oid) { obj = role.objects[j]; break; }
        }
      }
      if (!obj) { removeRLS(); return; }

      if (!isReadPermissionEnabled()) {
        removeRLS();
        console.log('[RLS Inject] Read permission disabled — section hidden');
        return;
      }

      var point = findInsertionPoint();
      if (!point) return;

      removeRLS();
      var el = buildRLSElement(obj.mode, roleId, oid);
      point.parent.insertBefore(el, point.before);
      console.log('[RLS Inject] Inline for', obj.labelSingular, '- mode:', obj.mode);
    });
  }

  var obsTarget = null;
  var readToggleObserver = new MutationObserver(function() {
    if (!document.getElementById('rls-inline-section')) return;
    if (!isReadPermissionEnabled()) {
      removeRLS();
      console.log('[RLS Inject] Read toggle changed to OFF — hiding');
    }
  });

  function startReadToggleWatch() {
    var toggles = document.querySelectorAll('[role="switch"], [class*="toggle"], input[type="checkbox"]');
    for (var i = 0; i < toggles.length; i++) {
      var parent = toggles[i].closest('[class*="row"], [class*="item"], div');
      if (parent) {
        var labelEl = parent.querySelector('span, label, p');
        if (labelEl) {
          var text = labelEl.textContent.toLowerCase().trim();
          if (text.indexOf('voir') !== -1 || text === 'see' || text === 'read' || text.indexOf('lecture') !== -1) {
            if (obsTarget !== toggles[i]) {
              obsTarget = toggles[i];
              readToggleObserver.observe(toggles[i], { attributes: true, attributeFilter: ['aria-checked', 'data-state', 'class'] });
              if (toggles[i].tagName === 'INPUT') {
                toggles[i].addEventListener('change', function() {
                  setTimeout(function() {
                    if (!isReadPermissionEnabled()) {
                      removeRLS();
                      console.log('[RLS Inject] Read checkbox changed to OFF — hiding');
                    } else {
                      lastUrl = '';
                    }
                  }, 100);
                });
              }
            }
            break;
          }
        }
      }
    }
  }

  function checkAndRemoveIfNavigatedAway() {
    var roleId = getRoleIdFromUrl();
    var oid = getObjectMetadataIdFromUrl();
    if (!roleId || !oid) {
      removeRLS();
      currentConfig = null;
      lastUrl = window.location.href;
    }
  }

  setInterval(function() {
    checkAndRemoveIfNavigatedAway();
    var roleId = getRoleIdFromUrl();
    var oid = getObjectMetadataIdFromUrl();
    if (roleId && oid) {
      tryInject();
      startReadToggleWatch();
    }
  }, POLL_INTERVAL);

  window.addEventListener('popstate', function() { lastUrl = ''; });

  console.log('[RLS Inject] Script loaded.');
})();
