// Front-end auth wired to Flask backend API (SQLite). LocalStorage mock removed.
(function(){
  const accountBtn = document.getElementById('accountBtn');
  const overlay = document.getElementById('authOverlay');
  const dialog = document.getElementById('authDialog');
  const closeBtn = document.getElementById('authClose');
  const tabs = Array.from(document.querySelectorAll('.auth-tab'));
  const panels = {
    login: document.getElementById('loginPanel'),
    signup: document.getElementById('signupPanel')
  };
  const loginForm = document.getElementById('loginForm');
  const signupForm = document.getElementById('signupForm');

  // Allow header-only use (some pages may omit overlay/dialog markup)
  if(!accountBtn) return; // No header button -> nothing to do

  // ========= Simple pub/sub + persistence =========
  const SUBS = [];
  let currentUser = null;
  function notify(){
    // Fire JS callbacks
    SUBS.forEach(fn=>{ try { fn(currentUser); } catch(e){} });
    // Fire DOM event for any listeners
    try { document.dispatchEvent(new CustomEvent('pm-auth-change',{ detail:{ user: currentUser }})); } catch(e){}
  }
  function persist(){
    try {
      if(currentUser) localStorage.setItem('pm_current_user', JSON.stringify(currentUser));
      else localStorage.removeItem('pm_current_user');
    } catch(e){}
  }

  // Optimistic UI from previous navigation (avoid flash of "Login")
  try {
    const cached = localStorage.getItem('pm_current_user');
    if(cached){
      const u = JSON.parse(cached);
      if(u && u.id){ currentUser = u; /* don't notify yet; updateAccountState after it's defined */ }
    }
  } catch(e){}

  function openAuth(initial){
    if(!overlay || !dialog){
      // If already logged in, go to account page; otherwise do nothing.
      if(accountBtn.classList.contains('logged-in')) window.location.href='account.html';
      return;
    }
    overlay.classList.add('open');
    dialog.classList.add('open');
    dialog.setAttribute('aria-hidden','false');
    overlay.setAttribute('aria-hidden','false');
    accountBtn.setAttribute('aria-expanded','true');
    if(initial) switchTab(initial);
    setTimeout(()=>{
      if(!dialog) return; const first = dialog.querySelector('.auth-panel.active input'); first && first.focus();
    },30);
    trapFocus();
  }
  function closeAuth(){
    if(!overlay || !dialog) return;
    overlay.classList.remove('open');
    dialog.classList.remove('open');
    dialog.setAttribute('aria-hidden','true');
    overlay.setAttribute('aria-hidden','true');
    accountBtn.setAttribute('aria-expanded','false');
    releaseFocus();
  }

  accountBtn.addEventListener('click', (e)=>{
    // If logged in and not clicking the logout dot, go to account page
    if(accountBtn.classList.contains('logged-in')){
      if(e.target.closest('.logout-dot')){
        return; // handled in separate listener
      }
      window.location.href = 'account.html';
      return;
    }
    if(dialog.classList.contains('open')){ closeAuth(); return; }
    openAuth('login');
  });
  overlay && overlay.addEventListener('click', closeAuth);
  closeBtn && closeBtn.addEventListener('click', closeAuth);
  document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeAuth(); });

  function switchTab(which){
    tabs.forEach(t=>{
      const active = t.dataset.target === (which==='login'?'loginPanel':'signupPanel');
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', String(active));
    });
    panels.login.classList.toggle('active', which==='login');
    panels.signup.classList.toggle('active', which==='signup');
    panels.login.toggleAttribute('hidden', which!=='login');
    panels.signup.toggleAttribute('hidden', which!=='signup');
  }
  tabs.forEach(t=> t.addEventListener('click', ()=>{
    switchTab(t.id==='loginTab'?'login':'signup');
  }));
  dialog && dialog.addEventListener('click', e=>{
    const sw = e.target.closest('[data-switch]');
    if(sw){ switchTab(sw.dataset.switch); }
  });

  // ========= API Helpers =========
  // Attempt to auto-detect backend origin. If you're opening index.html via a different dev server
  // (e.g. VS Code Live Server on another port) the relative "/api/..." calls will hit THAT server
  // (and return an HTML 404) -> JSON parse fails -> "Bad JSON". We fallback to 127.0.0.1:5000.
  const API_BASE = (function(){
    // If current page already on port 5000 (Flask default) we can stay same-origin.
    if(location.port === '5000') return '';
    // Allow manual override via global if user sets window.PM_API_BASE before this script.
    if(window.PM_API_BASE) return window.PM_API_BASE;
    return 'http://127.0.0.1:5000';
  })();

  async function api(path, opts={}){
    let res;
    try {
      res = await fetch(API_BASE + path, {
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', ...(opts.headers||{}) },
        ...opts
      });
    } catch (netErr) {
      throw new Error('Network error: could not reach API');
    }

    // Try to parse JSON; if it fails, capture body text for diagnostics.
    let text, data;
    try {
      text = await res.text();
      data = text ? JSON.parse(text) : {};
    } catch (parseErr) {
      console.error('[Auth API] Non-JSON response for', path, 'status=', res.status, 'body snippet=', (text||'').slice(0,250));
      throw new Error('Received non-JSON response (status '+res.status+'). Is the backend running on '+API_BASE+'?');
    }

    if(!res.ok || !data.ok){
      const errMsg = (data && (data.error || data.message)) || res.statusText || 'Request failed';
      throw new Error(errMsg);
    }
    return data;
  }
  function setCurrent(user){
    currentUser = user;
    updateAccountState();
    persist();
    notify();
  }
  function getCurrent(){ return currentUser; }
  async function logout(){
  try { await api('/api/auth/logout', { method:'POST' }); } catch(e){}
  setCurrent(null);
  // Force full page refresh so all components reset (header/account page/etc.)
  try { window.location.reload(); } catch(e){}
  }

  signupForm && signupForm.addEventListener('submit', async e=>{
    e.preventDefault();
    const fd = new FormData(signupForm);
    const name = (fd.get('name')||'').trim();
    const email = (fd.get('email')||'').trim().toLowerCase();
    const pass = fd.get('password')||'';
    const confirm = fd.get('confirm')||'';
    const msg = ensureMsg(signupForm);
    if(!name || !email || !pass) return setMsg(msg,'Please fill all fields');
    if(pass !== confirm) return setMsg(msg,'Passwords do not match');
    if(pass.length < 6) return setMsg(msg,'Password must be at least 6 characters');
    setMsg(msg,'Creating account...');
    try {
      const data = await api('/api/auth/signup', { method:'POST', body: JSON.stringify({ name, email, password: pass }) });
      setCurrent(data.user);
      setMsg(msg,'Account created! Redirecting...', true);
      setTimeout(()=>{ closeAuth(); }, 700);
    } catch(err){
      setMsg(msg, err.message || 'Signup failed');
    }
  });

  loginForm && loginForm.addEventListener('submit', async e=>{
    e.preventDefault();
    const fd = new FormData(loginForm);
    const email = (fd.get('email')||'').trim().toLowerCase();
    const pass = fd.get('password')||'';
    const msg = ensureMsg(loginForm);
    if(!email || !pass) return setMsg(msg,'Enter email & password');
    setMsg(msg,'Signing in...');
    try {
      const data = await api('/api/auth/login', { method:'POST', body: JSON.stringify({ email, password: pass }) });
      setCurrent(data.user);
  setMsg(msg,'Logged in!', true);
  // Refresh page so all components pick up authenticated state consistently
  setTimeout(()=>{ try{ window.location.reload(); }catch(e){ closeAuth(); } }, 350);
    } catch(err){
      setMsg(msg, err.message || 'Login failed');
    }
  });

  function ensureMsg(form){
    let m = form.querySelector('.auth-message');
    if(!m){ m = document.createElement('div'); m.className='auth-message'; form.appendChild(m); }
    return m;
  }
  function setMsg(el,text,success){ el.textContent=text; el.classList.toggle('success', !!success); }

  function updateAccountState(){
    const user = getCurrent();
    if(user){
      accountBtn.classList.add('logged-in');
      accountBtn.setAttribute('aria-label','Account for '+user.name);
      // Show only one initial (remove duplicate label initials)
      accountBtn.innerHTML = '<span class="account-icon" aria-hidden="true">'+escapeHtml(user.name.charAt(0).toUpperCase())+'</span>'+
        '<span class="logout-dot" title="Logout" style="margin-left:4px; font-size:14px; opacity:.7;">⏻</span>';
    } else {
      accountBtn.classList.remove('logged-in');
      accountBtn.setAttribute('aria-label','Sign in or create account');
      accountBtn.innerHTML = '<span class="account-icon" aria-hidden="true">'+defaultIcon()+'</span><span class="account-label">Login</span>';
    }
  }
  function shortName(n){ return n.split(/\s+/).slice(0,2).map(p=>p[0].toUpperCase()).join('').slice(0,2); }
  function defaultIcon(){ return '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12c2.761 0 5-2.239 5-5s-2.239-5-5-5-5 2.239-5 5 2.239 5 5 5Z"/><path d="M3.5 22c1.6-4.1 5-6 8.5-6s6.9 1.9 8.5 6"/></svg>'; }
  function escapeHtml(s){ return s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

  accountBtn.addEventListener('click', e=>{
    if(accountBtn.classList.contains('logged-in')){
      // If logged in and user clicks the small power icon, logout
      if(e.target.closest('.logout-dot')){ logout(); e.stopPropagation(); return; }
    }
  }, true);

  // Focus trapping
  let focusable = [];
  let firstEl, lastEl;
  function trapFocus(){
    focusable = Array.from(dialog.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter(el=>!el.hasAttribute('disabled'));
    firstEl = focusable[0];
    lastEl = focusable[focusable.length-1];
    document.addEventListener('keydown', focusTrapHandler);
  }
  function releaseFocus(){ document.removeEventListener('keydown', focusTrapHandler); }
  function focusTrapHandler(e){
    if(e.key !== 'Tab') return;
    if(focusable.length===0) return;
    if(e.shiftKey && document.activeElement===firstEl){ e.preventDefault(); lastEl.focus(); }
    else if(!e.shiftKey && document.activeElement===lastEl){ e.preventDefault(); firstEl.focus(); }
  }

  // Initialize: fetch session user
  // Perform initial paint (possibly from cached user) before network
  updateAccountState();
  (async function init(){
    try {
      const data = await api('/api/auth/me');
      setCurrent(data.user);
    } catch { /* keep optimistic state */ }
  })();

  // ========= Global exposure =========
  window.PMAuth = {
    getCurrent,
    open: openAuth,
    logout,
    onChange(fn){ if(typeof fn==='function'){ SUBS.push(fn); if(currentUser!==null) { try{ fn(currentUser);}catch(e){} } } return ()=>{ const i=SUBS.indexOf(fn); if(i>-1) SUBS.splice(i,1); }; },
    _debugSet: setCurrent
  };
})();
