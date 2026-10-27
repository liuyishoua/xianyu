let workspaceIdentity = null;
let workspaceIdentityRequested = false;
let workspaceIdentityInFlight = false;
/* Visual shell only: no platform requests, account changes, or task execution. */
const root = document.documentElement;
const paths = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
  search: '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.5 4.5"/>',
  library: '<path d="M4 5h6l2 3h8v12H4z"/><path d="M4 11h16"/>',
  drafts: '<path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h5"/>',
  tools: '<path d="M14 5a5 5 0 0 0-6 6L3 16l5 5 5-5a5 5 0 0 0 6-6l-3 3-4-4z"/>',
  jobs: '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4M16 3v4M4 11h16M8 15h3M8 18h6"/>',
  account: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
  moon: '<path d="M20.5 13A9 9 0 0 1 11 3.5 9 9 0 1 0 20.5 13z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
  collapse: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M16 9l-3 3 3 3"/>',
  expand: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M13 9l3 3-3 3"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  arrow: '<path d="M5 12h14M14 7l5 5-5 5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
};
const aliases = {
  overview: 'overview', products: 'library', plans: 'jobs', inbox: 'drafts', agents: 'tools',
  execution: 'jobs', interaction: 'drafts', review: 'overview', accounts: 'account',
  messages: 'drafts', publish: 'drafts', templates: 'drafts', monitoring: 'jobs',
};
const icon = key => `<svg class="workspace-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${paths[key] || paths[aliases[key]] || paths.library}</svg>`;
const remember = (key, value) => { try { localStorage.setItem(key, value); } catch { /* optional */ } };
const mobile = () => matchMedia('(max-width: 760px)').matches;
let opener = null;
let queued = false;

function control(action, label, symbol) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'workspace-control';
  button.dataset.workspaceAction = action;
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML = icon(symbol);
  button.dataset.workspaceSymbol = symbol;
  return button;
}

function updateControls() {
  const dark = root.dataset.workspaceTheme !== 'light';
  document.querySelectorAll('[data-workspace-action="theme"]').forEach(button => {
    const label = dark ? '切换为浅色模式' : '切换为深色模式';
    const symbol = dark ? 'sun' : 'moon';
    if (button.dataset.workspaceSymbol !== symbol) {
      button.innerHTML = icon(symbol);
      button.dataset.workspaceSymbol = symbol;
    }
    button.setAttribute('aria-label', label);
    button.title = label;
  });
  const collapsed = root.classList.contains('workspace-sidebar-collapsed');
  document.querySelectorAll('[data-workspace-action="collapse"]').forEach(button => {
    const symbol = collapsed ? 'expand' : 'collapse';
    if (button.dataset.workspaceSymbol !== symbol) {
      button.innerHTML = icon(symbol);
      button.dataset.workspaceSymbol = symbol;
    }
    button.title = collapsed ? '展开侧栏' : '收起侧栏';
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-expanded', String(!collapsed));
  });
  document.querySelectorAll('.workspace-mobile-menu').forEach(button => {
    button.setAttribute('aria-expanded', String(root.classList.contains('workspace-menu-open')));
  });
}

function closeMenu(restoreFocus = false) {
  root.classList.remove('workspace-menu-open');
  const overlay = document.querySelector('.workspace-nav-backdrop');
  if (overlay) overlay.hidden = true;
  updateControls();
  if (restoreFocus && opener?.isConnected) opener.focus();
}

function openMenu(button) {
  opener = button;
  root.classList.add('workspace-menu-open');
  let overlay = document.querySelector('.workspace-nav-backdrop');
  if (!overlay) {
    overlay = control('close-menu', '关闭导航', 'close');
    overlay.className = 'workspace-nav-backdrop';
    document.body.append(overlay);
  }
  overlay.hidden = false;
  updateControls();
  document.querySelector('.sidebar nav button, .sidebar nav a, .sidebar .navlink')?.focus();
}

function decorateSidebar(sidebar) {
  if (!sidebar.querySelector('[data-workspace-hub]')) {
    const link = document.createElement('a');
    link.dataset.workspaceHub = 'true';
    link.href = 'https://d.58nft.net:9461/platform-select';
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.className = 'workspace-hub-link';
    link.title = '应用中心 · 切换平台';
    link.innerHTML = `${icon('overview')}<span class="workspace-nav-label">应用中心 ↗</span>`;
    (sidebar.querySelector('.sidebar-bottom') || sidebar).append(link);
  }

  if (!sidebar.dataset.workspaceReady) {
  sidebar.dataset.workspaceReady = 'true';
  sidebar.setAttribute('aria-label', '工作台侧栏');
  const brand = sidebar.querySelector('.brand');
  if (brand) {
    for (const child of [...brand.childNodes]) {
      if (child.nodeType === Node.TEXT_NODE && child.textContent.trim()) {
        const label = document.createElement('span');
        label.className = 'workspace-brand-label';
        label.textContent = child.textContent.trim();
        child.replaceWith(label);
      }
    }
    brand.querySelectorAll(':scope > span:not(.brand-icon):not(.brandmark)').forEach(label => label.classList.add('workspace-brand-label'));
    if (!brand.querySelector('.brand-icon, .brandmark')) {
      const mark = document.createElement('span');
      mark.className = 'brand-icon workspace-brand-mark';
      mark.textContent = document.title.toLowerCase().includes('douyin') ? '抖' : 'W';
      brand.prepend(mark);
    }
  }
  sidebar.append(control('collapse', '收起侧栏', 'collapse'));
  }
  const nav = sidebar.querySelector('nav');
  nav?.setAttribute('aria-label', '工作台导航');
  sidebar.querySelectorAll('nav button, .navlink').forEach(button => {
    if (button.classList.contains('active')) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
    if (button.dataset.workspaceNavReady) return;
    button.dataset.workspaceNavReady = 'true';
    let key = button.dataset.page || button.dataset.route || button.dataset.pageId || button.dataset.nav || '';
    if (!key && button.matches('a')) key = (button.getAttribute('href') || '').split('/').filter(Boolean).pop() || 'overview';
    if (!key) key = button.textContent.includes('总览') ? 'overview' : 'tools';
    const labelText = [...button.childNodes].filter(node => !(node.nodeType === Node.ELEMENT_NODE && node.matches('.num, [aria-hidden="true"], svg'))).map(node => node.textContent).join('').trim();
    button.title = labelText;
    if (button.classList.contains('active')) button.setAttribute('aria-current', 'page');
    const number = button.querySelector('.num');
    if (number) number.remove();
    if (!button.querySelector('svg')) {
      const label = document.createElement('span');
      label.className = 'workspace-nav-label';
      label.textContent = labelText;
      button.replaceChildren(label);
      button.insertAdjacentHTML('afterbegin', icon(key));
    } else {
      for (const child of [...button.childNodes]) {
        if (child.nodeType === Node.TEXT_NODE && child.textContent.trim()) {
          const label = document.createElement('span');
          label.className = 'workspace-nav-label';
          label.textContent = child.textContent;
          child.replaceWith(label);
        }
      }
      button.querySelectorAll(':scope > span:not(.nav-dot)').forEach(label => label.classList.add('workspace-nav-label'));
    }
  });
}

function decorateHeader(header) {
  if (header.dataset.workspaceReady) return;
  header.dataset.workspaceReady = 'true';
  let right = header.querySelector('.topbar-right');
  if (!right) {
    right = document.createElement('div');
    right.className = 'topbar-right';
    const account = header.querySelector('#account');
    if (account) right.append(account);
    header.append(right);
  }
  const menu = control('menu', '打开导航', 'menu');
  menu.classList.add('workspace-mobile-menu');
  menu.setAttribute('aria-controls', 'workspace-sidebar');
  header.prepend(menu);
  right.prepend(control('theme', '切换为浅色模式', 'sun'));
}

function decorateContent() {
  document.querySelectorAll('.stats .stat, .metrics .metric').forEach((card, index) => {
    if (card.dataset.workspaceReady || card.querySelector('.stat-icon')) return;
    card.dataset.workspaceReady = 'true';
    const mark = document.createElement('span');
    mark.className = 'workspace-stat-icon';
    mark.innerHTML = icon(['library', 'drafts', 'jobs', 'account'][index % 4]);
    card.prepend(mark);
  });
  document.querySelectorAll('table').forEach(table => {
    if (table.parentElement?.matches('.tablewrap, .table-wrap, .workspace-table-scroll')) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'workspace-table-scroll';
    table.before(wrapper);
    wrapper.append(table);
  });
}

function decorate() {
  queued = false;
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    sidebar.id = 'workspace-sidebar';
    decorateSidebar(sidebar);
    if (!workspaceIdentityRequested) { workspaceIdentityRequested = true; void loadWorkspaceIdentity(); }
  }
  document.querySelectorAll('.topbar').forEach(decorateHeader);
  if (document.querySelector('.login-card') && !document.querySelector('.workspace-login-theme')) {
    const toggle = control('theme', '切换为浅色模式', 'sun');
    toggle.classList.add('workspace-login-theme');
    document.body.append(toggle);
  }
  decorateContent();
  updateControls();
  showWorkspaceIdentity();
}

function schedule() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(decorate);
}

// Capture only our own controls. Existing platform action handlers remain intact.
document.addEventListener('click', event => {
  const button = event.target.closest('[data-workspace-action]');
  if (!button) {
    if (mobile() && event.target.closest('.sidebar nav button, .sidebar nav a, .sidebar .navlink')) closeMenu();
    return;
  }
  event.preventDefault();
  event.stopImmediatePropagation();
  switch (button.dataset.workspaceAction) {
    case 'theme': {
      const next = root.dataset.workspaceTheme === 'light' ? 'dark' : 'light';
      root.dataset.workspaceTheme = next;
      remember('workspace-theme', next);
      remember('theme-mode', next);
      updateControls();
      break;
    }
    case 'collapse':
      root.classList.toggle('workspace-sidebar-collapsed');
      remember('workspace-sidebar-collapsed', String(root.classList.contains('workspace-sidebar-collapsed')));
      updateControls();
      break;
    case 'menu':
      if (root.classList.contains('workspace-menu-open')) closeMenu(true);
      else openMenu(button);
      break;
    case 'close-menu': closeMenu(true); break;
    case 'logout': void logoutWorkspace(); break;
  }
}, true);

document.addEventListener('keydown', event => {
  if (!mobile() || !root.classList.contains('workspace-menu-open')) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeMenu(true);
  } else if (event.key === 'Tab') {
    const focusable = [...document.querySelectorAll('.sidebar button, .sidebar a')].filter(el => el.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
matchMedia('(max-width: 760px)').addEventListener('change', () => closeMenu());
new MutationObserver(schedule).observe(document.querySelector('#app') || document.body, { childList: true, subtree: true });
decorate();

// Show the verified operating user separately from the connected platform account.
async function loadWorkspaceIdentity() {
  if (!document.querySelector('.sidebar') || workspaceIdentityInFlight) return;
  workspaceIdentityInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/api/workspace-identity', { signal: controller.signal });
    if (response.status === 401) {
      window.location.assign('https://d.58nft.net:9461/platform-select'); return;
    }
    if (!response.ok) return;
    const value = await response.json();
    if (workspaceIdentity && workspaceIdentity.owner.id !== value.owner.id) { window.location.reload(); return; }
    workspaceIdentity = value;
    showWorkspaceIdentity();
  } catch { /* Keep the current page on transient failures; APIs enforce access. */ }
  finally { clearTimeout(timeout); workspaceIdentityInFlight = false; }
}
function showWorkspaceIdentity() {
  const bar = document.querySelector('.topbar');
  if (!bar || !workspaceIdentity || bar.querySelector('.workspace-owner')) return;
  const label = document.createElement('a');
  label.className = 'workspace-owner'; label.href = workspaceIdentity.hub_url;
  label.textContent = '运营账号：' + workspaceIdentity.owner.username;
  label.title = '当前平台子账号与资料归此运营账号管理';
  bar.prepend(label);
  if (!bar.querySelector('[data-action="logout"], [data-workspace-action="logout"]')) {
    const logout = document.createElement('button'); logout.dataset.workspaceAction = 'logout';
    logout.textContent = '退出'; logout.title = '退出统一运营账号'; bar.append(logout);
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadWorkspaceIdentity(); });
window.addEventListener('focus', loadWorkspaceIdentity);
setTimeout(loadWorkspaceIdentity, 700);

async function logoutWorkspace() {
  try {
    const response = await fetch('/api/logout', {method:'POST',headers:{'Content-Type':'application/json','X-App-Request':'1'},body:'{}'});
    if (!response.ok) throw Error('退出未完成，请稍后重试');
    window.location.assign('https://d.58nft.net:9461/platform-select');
  } catch (error) {
    const bar = document.querySelector('.topbar');
    if (bar) { const message = document.createElement('span'); message.setAttribute('role','alert'); message.textContent = error.message; bar.append(message); }
  }
}

window.addEventListener('storage', event => { if (event.key === 'workspace-theme' || event.key === 'theme-mode' || event.key === null) updateControls(); });
