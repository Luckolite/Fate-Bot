function emptyActivityLogState(archive = null) {
  return {
    entries: [],
    nextBefore: null,
    hasMore: false,
    reason: null,
    warning: null,
    source: null,
    archive,
    matchMode: null,
    searchedQuery: null,
  };
}

const state = {
  session: null,
  guilds: [],
  guildId: null,
  settings: null,
  channels: [],
  roles: [],
  modules: {},
  modulesLive: false,
  editingModule: null,
  activityLogs: emptyActivityLogState(),
  loggerEditorSource: 'controls',
  quickEdit: null,
  profile: null,
  rankScope: 'server',
  board: 'guild',
  ownerSettings: null,
  ownerSettingsRevision: null,
  ownerSettingsLoaded: false,
  ownerSettingsLoading: false,
  ownerMemory: null,
  ownerMemoryHistory: [],
  ownerMemoryLoading: false,
};

const byId = id => document.getElementById(id);
const authGate = byId('auth-gate');
const dashboardShell = byId('dashboard-shell');
const loadingState = byId('loading-state');
const controlContent = byId('control-content');
const toast = byId('toast');
const dialogReturnFocus = new Map();
let pendingConfirmation = null;
let activityLogRequest = 0;
let activityLogSearchTimer = 0;
let modmailEntryRequest = 0;
let ownerMemoryTimer = 0;

function clearSensitiveDashboardState() {
  state.session = null;
  state.settings = null;
  state.channels = [];
  state.roles = [];
  state.modules = {};
  state.activityLogs = emptyActivityLogState();
  state.profile = null;
  state.ownerSettings = null;
  state.ownerSettingsRevision = null;
  state.ownerSettingsLoaded = false;
  state.ownerSettingsLoading = false;
  state.ownerMemory = null;
  state.ownerMemoryHistory = [];
  state.ownerMemoryLoading = false;
  stopOwnerMemoryRefresh();
  byId('owner-form').reset();
  byId('owner-activity-status').value = '';
  byId('owner-max-cached-messages').value = '';
  byId('owner-theme-color-value').textContent = '';
  byId('owner-nav').hidden = true;
  byId('owner-panel').hidden = true;
  dashboardShell.hidden = true;
  controlContent.hidden = true;
  document.body.classList.remove('owner-session');
}

window.addEventListener('pagehide', clearSensitiveDashboardState);
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  clearSensitiveDashboardState();
  location.reload();
});

const dashboardAuthChannel = typeof BroadcastChannel === 'function'
  ? new BroadcastChannel('fate-dashboard-auth')
  : null;
dashboardAuthChannel?.addEventListener('message', event => {
  if (event.data?.type !== 'logout') return;
  clearSensitiveDashboardState();
  location.replace('/');
});

const moduleCatalog = {
  protection: {
    label: 'Protection',
    copy: 'Quietly stop abuse before staff has to react.',
    modules: ['verification', 'chatfilter', 'anti_spam', 'anti_raid'],
  },
  community: {
    label: 'Community',
    copy: 'Automate joining, leaving, roles, events, and member choice.',
    modules: ['autorole', 'welcome', 'leave', 'restore_roles', 'selfroles', 'giveaways', 'starboard'],
  },
  records: {
    label: 'Records',
    copy: 'Keep the right staff activity visible and organized.',
    modules: ['logger', 'modmail', 'vc_log'],
  },
};

const moduleMeta = {
  verification: { title: 'Member Verification', icon: '◆', tone: 'violet', copy: 'Ask new members to complete a private captcha before receiving access.' },
  autorole: { title: 'Auto Roles', icon: '↗', tone: 'violet', copy: 'Give new members one or more roles automatically.' },
  chatfilter: { title: 'Chat Filter', icon: '⌁', tone: 'rose', copy: 'Remove blocked phrases, phishing links, and other unwanted messages.' },
  logger: { title: 'Logging', icon: '≡', tone: 'blue', copy: 'Keep important server activity visible and organized for staff.' },
  modmail: { title: 'Modmail', icon: '✉', tone: 'violet', copy: 'Give members a private way to contact your staff team.' },
  anti_spam: { title: 'Spam Protection', icon: '⌁', tone: 'amber', copy: 'Stop message floods, repeated content, and mass mentions.' },
  anti_raid: { title: 'Raid Protection', icon: '◆', tone: 'red', copy: 'Respond to sudden joins, mass bans, and suspicious accounts.' },
  welcome: { title: 'Welcome Messages', icon: '✦', tone: 'mint', copy: 'Greet new members with a channel, message, and images.' },
  leave: { title: 'Goodbye Messages', icon: '←', tone: 'slate', copy: 'Post a farewell when a member leaves.' },
  restore_roles: { title: 'Restore Roles', icon: '↺', tone: 'violet', copy: 'Return safe roles when a former member rejoins.' },
  selfroles: { title: 'Self-Assign Roles', icon: '☷', tone: 'blue', copy: 'Let members choose their own roles from a menu.' },
  giveaways: { title: 'Giveaways', icon: '◇', tone: 'mint', copy: 'Run button-powered prize events with eligibility rules and automatic winners.' },
  starboard: { title: 'Starboards', icon: '✦', tone: 'amber', copy: 'Promote community favorites with named boards and any reaction emoji.' },
  vc_log: { title: 'Voice Activity Log', icon: '◉', tone: 'mint', copy: 'Record voice channel joins, leaves, and moves.' },
};

const antiSpamModules = [
  ['rate_limit', 'Flood control', 'Rolling per-member message limits stop fast floods.'],
  ['mass_pings', 'Mentions & ghost pings', 'Limits mention bursts and reports deleted pings.'],
  ['duplicates', 'Repeats, links & media', 'Finds normalized repeats, canonical links, perceptual image matches, stickers, and thread spam.'],
  ['inhuman', 'Message integrity', 'Catches character walls, vertical spam, invisible text, and risky paste floods.'],
  ['anti_macro', 'Automation cadence', 'Detects long, unnaturally regular posting patterns.'],
  ['evasion', 'Evasion resistance', 'Finds invisible Unicode, Zalgo, spoiler walls, edit churn, and delete-resend behavior.'],
  ['coordinated', 'Coordinated campaigns', 'Clusters the same normalized payload across members and channels.'],
];

const antiSpamPolicies = [
  ['default', 'Default response', 'Inherited by checks without an override.'],
  ['rate_limit', 'Flood control', 'Rolling message-rate incidents.'],
  ['mass_pings', 'Mention bursts', 'Repeated mention-bearing messages.'],
  ['mass_pings_per_msg', 'Mentions per message', 'Too much mention weight in one message.'],
  ['ghost_pings', 'Ghost pings', 'Mention messages deleted by their author.'],
  ['duplicates', 'Repeated text', 'Normalized text repeated over time.'],
  ['duplicate_segments', 'Repeated words', 'One message dominated by repeated segments.'],
  ['same_link', 'Repeated destinations', 'Canonical links posted repeatedly.'],
  ['same_image', 'Repeated images', 'Visually matching image uploads.'],
  ['sticker', 'Sticker bursts', 'Rapid sticker posting.'],
  ['same_sticker', 'Repeated sticker', 'The same sticker posted repeatedly.'],
  ['inhuman', 'Message integrity', 'Malformed text and character-wall checks.'],
  ['copy_paste', 'Paste risk', 'Large paste with no recent typing evidence.'],
  ['anti_macro', 'Automation cadence', 'Unnaturally regular message timing.'],
  ['evasion', 'Evasion resistance', 'Invisible text, marks, spoilers, and edits.'],
  ['edit_churn', 'Rapid edits', 'Repeated message editing.'],
  ['delete_resend', 'Delete and resend', 'Repeated content after self-deletion.'],
  ['coordinated', 'Coordinated campaign', 'Same payload across several accounts.'],
];

function syncVerificationModule() {
  if (!state.settings?.verification) return;
  const access = state.modules.verification || {};
  state.modules.verification = {
    available: true,
    ...state.settings.verification,
    locked: Boolean(access.locked),
    required_permission: access.required_permission || 'Manage Server',
  };
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (state.session?.csrf && !['GET', undefined].includes(options.method)) {
    headers['X-CSRF-Token'] = state.session.csrf;
  }
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.error || 'Something went wrong. Please try again.');
    error.status = response.status;
    throw error;
  }
  return payload;
}

let toastTimer;
function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast show${isError ? ' error' : ''}`;
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3500);
}

const cosmicMotionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
let cosmicScrollFrame = 0;
function renderCosmicParallax() {
  cosmicScrollFrame = 0;
  const style = document.documentElement.style;
  if (cosmicMotionPreference.matches) {
    for (const property of ['--stars-far-scroll', '--stars-mid-scroll', '--stars-near-scroll', '--nebula-scroll', '--nebula-scroll-reverse', '--nebula-scroll-lower', '--planet-scroll', '--moon-scroll', '--constellation-scroll', '--constellation-scroll-reverse']) style.removeProperty(property);
    return;
  }
  const scroll = Math.max(0, window.scrollY);
  style.setProperty('--stars-far-scroll', `${scroll * -.018}px`);
  style.setProperty('--stars-mid-scroll', `${scroll * -.034}px`);
  style.setProperty('--stars-near-scroll', `${scroll * -.058}px`);
  style.setProperty('--nebula-scroll', `${scroll * -.012}px`);
  style.setProperty('--nebula-scroll-reverse', `${scroll * .0084}px`);
  style.setProperty('--nebula-scroll-lower', `${scroll * -.0066}px`);
  style.setProperty('--planet-scroll', `${scroll * -.026}px`);
  style.setProperty('--moon-scroll', `${scroll * .017}px`);
  style.setProperty('--constellation-scroll', `${scroll * -.044}px`);
  style.setProperty('--constellation-scroll-reverse', `${scroll * .033}px`);
}
function requestCosmicParallax() {
  if (!cosmicScrollFrame) cosmicScrollFrame = requestAnimationFrame(renderCosmicParallax);
}
window.addEventListener('scroll', requestCosmicParallax, { passive: true });
cosmicMotionPreference.addEventListener('change', requestCosmicParallax);
renderCosmicParallax();

function openDialog(id) {
  const dialog = byId(id);
  dialogReturnFocus.set(id, document.activeElement);
  dialog.hidden = false;
  document.body.classList.add('dialog-open');
  requestAnimationFrame(() => {
    const firstControl = dialog.querySelector('.dialog-body input:not([disabled]), .dialog-body select:not([disabled]), .dialog-body textarea:not([disabled]), .dialog-actions .button-primary:not([disabled]), .dialog-close');
    firstControl?.focus({ preventScroll: true });
  });
}

function closeDialog(id) {
  const dialog = byId(id);
  dialog.hidden = true;
  if (![...document.querySelectorAll('.dashboard-dialog')].some(item => !item.hidden)) {
    document.body.classList.remove('dialog-open');
  }
  const returnFocus = dialogReturnFocus.get(id);
  dialogReturnFocus.delete(id);
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}

function cancelConfirmation() {
  pendingConfirmation = null;
  closeDialog('confirmation-dialog');
}

function requestConfirmation({ title, copy, confirmLabel = 'Confirm', action }) {
  pendingConfirmation = action;
  byId('confirmation-title').textContent = title;
  byId('confirmation-copy').textContent = copy;
  byId('confirmation-accept').textContent = confirmLabel;
  openDialog('confirmation-dialog');
}

function initials(name) {
  return name.split(/\s+/).map(part => part[0]).join('').slice(0, 2).toUpperCase();
}

function formatScore(score, board = state.board) {
  return `${Number(score).toLocaleString()} ${board === 'commands' ? 'uses' : 'XP'}`;
}

function channelOptions(select, value, { current = false, disabled = true, emptyLabel = 'Disabled' } = {}) {
  select.replaceChildren();
  if (disabled) select.add(new Option(emptyLabel, '__off'));
  if (current) select.add(new Option('Use the current channel', '__current'));
  for (const channel of state.channels) select.add(new Option(`# ${channel.name}`, channel.id));
  if (![...select.options].some(option => option.value === String(value))) {
    if (value) select.add(new Option(`Unavailable channel · ${value}`, String(value)));
  }
  select.value = value === true ? '__current' : value ? String(value) : '__off';
}

function roleOptions(select, value, emptyLabel) {
  select.replaceChildren();
  select.add(new Option(emptyLabel, '__off'));
  for (const role of state.roles) select.add(new Option(`@ ${role.name}`, role.id));
  if (![...select.options].some(option => option.value === String(value))) {
    if (value) select.add(new Option(`Unavailable role · ${value}`, String(value)));
  }
  select.value = value ? String(value) : '__off';
}

function selectedChannel(value) {
  if (value === '__off') return false;
  if (value === '__current') return true;
  return value;
}

function setSelectOptions(select, items, selected = [], prefix = '') {
  const chosen = new Set((selected || []).map(String));
  select.replaceChildren();
  for (const item of items) {
    const option = new Option(`${prefix}${item.name}`, item.id);
    option.selected = chosen.has(String(item.id));
    select.add(option);
  }
  enableClickToggleMultiSelect(select);
}

function selectedValues(select) {
  return [...select.selectedOptions].map(option => option.value);
}

function enableClickToggleMultiSelect(select) {
  if (!select.multiple || select.dataset.clickToggle === 'true') return;
  select.dataset.clickToggle = 'true';
  select.addEventListener('mousedown', event => {
    if (event.target.tagName !== 'OPTION') return;
    event.preventDefault();
    select.focus();
    event.target.selected = !event.target.selected;
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function renderChannelChips(containerId, selectedChannels) {
  const selected = new Set((selectedChannels || []).map(String));
  const container = byId(containerId);
  container.replaceChildren();
  if (!state.channels.length) {
    const note = document.createElement('p');
    note.textContent = 'No channels are available right now. Make sure Fate is connected to this server, then refresh the page.';
    container.append(note);
  }
  for (const channel of state.channels) {
    const label = document.createElement('label');
    label.className = 'channel-chip';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = channel.id;
    input.checked = selected.has(channel.id);
    const text = document.createElement('span');
    text.textContent = channel.name;
    label.append(input, text);
    container.append(label);
  }
}

function splitLines(value) {
  return [...new Set(value.split(/\r?\n/).map(item => item.trim()).filter(Boolean))];
}

function toggleSetting(id, title, copy) {
  return `<label class="dialog-setting toggle-setting"><span><strong>${title}</strong><small>${copy}</small></span><span class="switch"><input id="${id}" type="checkbox"><span></span></span></label>`;
}

function selectSetting(id, title, copy, multiple = false) {
  const guidance = multiple ? ' Click each item to add or remove it.' : '';
  return `<label class="dialog-setting dialog-setting-stack"><span><strong>${title}</strong><small>${copy}${guidance}</small></span><select id="${id}"${multiple ? ' multiple size="5"' : ''}></select></label>`;
}

function textSetting(id, title, copy, placeholder = '') {
  return `<label class="dialog-setting dialog-setting-stack"><span><strong>${title}</strong><small>${copy}</small></span><textarea id="${id}" rows="4" placeholder="${placeholder}"></textarea></label>`;
}

function moduleSummary(name, config) {
  if (!config?.available) return 'Temporarily unavailable';
  if (name === 'verification') {
    if (!config.enabled) return 'Ready to set up';
    return config.panel_active ? 'Verification message active' : 'Save to post its message';
  }
  if (name === 'autorole') return config.roles.length ? `${config.roles.length} role${config.roles.length === 1 ? '' : 's'} selected` : 'Choose at least one role';
  if (name === 'chatfilter') return `${config.blacklist.length} filtered phrase${config.blacklist.length === 1 ? '' : 's'}`;
  if (name === 'logger') {
    const summary = config.channel_id ? `${config.groups.length} logging groups selected` : 'Choose a logging channel';
    return config.local_archive?.enabled ? `${summary} · saved history on` : summary;
  }
  if (name === 'modmail') {
    const channel = state.channels.find(item => item.id === String(config.channel_id));
    return channel ? `#${channel.name}` : 'Choose a staff channel';
  }
  if (name === 'anti_spam') {
    const count = antiSpamModules.filter(([key]) => config.protections?.[key]?.enabled).length;
    const mode = { observe: 'Observe', delete: 'Delete only', enforce: 'Enforce' }[config.mode] || 'Enforce';
    return `${count}/7 protections · ${mode}`;
  }
  if (name === 'anti_raid') {
    if (config.health?.runtime?.active_lockdown) return 'Lockdown active';
    const count = Object.values(config.protections || {}).filter(protection => protection.enabled).length;
    return `${count}/3 defenses · ${(config.mode || 'enforce') === 'observe' ? 'Observe' : 'Enforce'}`;
  }
  if (name === 'welcome' || name === 'leave' || name === 'vc_log') {
    const channel = state.channels.find(item => item.id === String(config.channel_id));
    return channel ? `#${channel.name}` : 'Choose a channel';
  }
  if (name === 'restore_roles') return config.allow_permissions ? 'Includes moderation roles' : 'Safe roles only';
  if (name === 'selfroles') return `${config.menus.length} active menu${config.menus.length === 1 ? '' : 's'}`;
  if (name === 'giveaways') {
    const active = Number(config.active_count || 0);
    return active
      ? `${active} active · ${Number(config.total_entries || 0)} entr${Number(config.total_entries || 0) === 1 ? 'y' : 'ies'}`
      : 'Ready for your next event';
  }
  if (name === 'starboard') {
    const active = config.boards.filter(board => board.enabled).length;
    if (!config.boards.length) return 'Add your first board';
    return `${active}/${config.boards.length} board${config.boards.length === 1 ? '' : 's'} active`;
  }
  return 'Ready to set up';
}

function moduleSetupIssue(name, config) {
  if (!config?.available) {
    return { target: 'modules', title: `${moduleMeta[name].title} is unavailable`, copy: 'This feature is temporarily unavailable. Try again later.' };
  }
  if (!config.enabled) return null;
  if (name === 'verification' && !config.channel_id) return { target: name, title: 'Choose a verification channel', copy: 'Select where new members should start verification.' };
  if (name === 'verification' && !config.verified_role_id) return { target: name, title: 'Choose a verified role', copy: 'Select the role members receive after completing the captcha.' };
  if (name === 'verification' && !config.panel_active) return { target: name, title: 'Post the verification message', copy: 'Verification is on, but members do not have a message to use yet.' };
  if (name === 'autorole' && !config.roles.length) return { target: name, title: 'Choose a role to assign', copy: 'Auto Roles needs at least one role before it can work.' };
  if (name === 'logger' && !config.channel_id) return { target: name, title: 'Choose a logging channel', copy: 'Select the staff channel where Fate should post server records.' };
  if (name === 'logger' && !config.groups.length) return { target: name, title: 'Choose what to record', copy: 'Select at least one type of server activity.' };
  if (name === 'modmail' && !config.channel_id) return { target: name, title: 'Choose a Modmail channel', copy: 'Select the private staff channel where Modmail conversations should appear.' };
  if (name === 'anti_spam' && !antiSpamModules.some(([key]) => config.protections?.[key]?.enabled)) return { target: name, title: 'Choose a spam check', copy: 'Turn on at least one type of spam protection.' };
  if (name === 'anti_raid' && !Object.values(config.protections || {}).some(protection => protection.enabled)) return { target: name, title: 'Choose a raid defense', copy: 'Enable join bursts, new-account clusters, or the compromised-staff guard.' };
  if (['welcome', 'leave', 'vc_log'].includes(name) && !config.channel_id) return { target: name, title: `${moduleMeta[name].title} needs a channel`, copy: 'Choose where Fate should post these messages.' };
  if (name === 'starboard' && !config.boards.some(board => board.enabled)) return { target: name, title: 'Activate a starboard', copy: 'Add or resume a board so reactions can promote messages.' };
  return null;
}

function overviewFeatureState(name) {
  const config = state.modules[name];
  if (!config?.available) return 'unavailable';
  if (config.locked) return 'locked';
  if (!config.enabled) return 'off';
  return moduleSetupIssue(name, config) ? 'attention' : 'ready';
}

function renderOverviewModules() {
  if (!state.settings || !Object.keys(state.modules).length) return;
  const essentials = [
    { name: 'anti_spam', label: 'Message safety', layout: 'hero' },
    { name: 'anti_raid', label: 'Server safety', layout: 'wide' },
    { name: 'logger', label: 'Staff records', layout: 'standard' },
    { name: 'welcome', label: 'New members', layout: 'standard' },
  ];
  const grid = byId('overview-core-grid');
  if (!grid) return;
  grid.replaceChildren();

  for (const essential of essentials) {
    const { name, label, layout } = essential;
    const config = state.modules[name];
    const meta = moduleMeta[name];
    const featureState = overviewFeatureState(name);
    const statusLabel = featureState === 'ready'
      ? 'Ready'
      : featureState === 'attention'
        ? 'Finish setup'
        : featureState === 'off'
          ? 'Not active'
          : featureState === 'locked' ? 'Locked' : 'Unavailable';
    const setupSummary = config?.locked
      ? `Requires ${config.required_permission}`
      : config ? moduleSummary(name, config) : 'Settings unavailable';

    const card = document.createElement('button');
    card.type = 'button';
    card.className = `overview-core-card ${meta.tone} ${featureState} ${layout}`;
    card.disabled = !config?.available || config.locked;
    card.setAttribute(
      'aria-label',
      `${meta.title}. ${statusLabel}. ${setupSummary}.${config?.locked ? '' : ' Open settings.'}`,
    );
    if (config?.locked) card.title = `Requires ${config.required_permission}`;

    const top = document.createElement('span');
    top.className = 'overview-core-top';
    const icon = document.createElement('span');
    icon.className = 'overview-core-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = meta.icon;
    const status = document.createElement('span');
    status.className = 'overview-core-state';
    const statusDot = document.createElement('i');
    statusDot.setAttribute('aria-hidden', 'true');
    status.append(statusDot, statusLabel);
    top.append(icon, status);

    const copy = document.createElement('span');
    copy.className = 'overview-core-copy';
    const kicker = document.createElement('small');
    kicker.textContent = label;
    const title = document.createElement('strong');
    title.textContent = meta.title;
    const description = document.createElement('span');
    description.textContent = meta.copy;
    copy.append(kicker, title, description);

    const footer = document.createElement('span');
    footer.className = 'overview-core-footer';
    const current = document.createElement('span');
    current.className = 'overview-core-current';
    const currentLabel = document.createElement('small');
    currentLabel.textContent = 'Current setup';
    const currentValue = document.createElement('strong');
    currentValue.textContent = setupSummary;
    current.append(currentLabel, currentValue);
    const open = document.createElement('span');
    open.className = 'overview-core-open';
    open.innerHTML = '<span>Configure</span><i aria-hidden="true">→</i>';
    footer.append(current, open);

    card.append(top, copy, footer);
    card.addEventListener('click', () => openModule(name));
    grid.append(card);
  }
}

function renderModules() {
  const container = byId('module-sections');
  container.replaceChildren();
  const note = byId('module-sync-note');
  const previewAvailable = Object.values(state.modules).some(config => config.available);
  note.classList.toggle('preview', !state.modulesLive);
  note.classList.toggle('offline', !state.modulesLive && !previewAvailable);
  note.querySelector('strong').textContent = state.modulesLive
    ? 'Ready to manage'
    : previewAvailable ? 'Demo server' : 'Settings unavailable';
  note.querySelector('small').textContent = state.modulesLive
    ? 'Changes take effect as soon as you save.'
    : previewAvailable
      ? 'Try any setting here. Demo changes do not affect a real Discord server.'
      : 'Fate cannot reach this server right now. Try again in a moment.';

  const moduleNames = Object.keys(moduleMeta);
  const enabledModules = moduleNames.filter(name => state.modules[name]?.available && state.modules[name].enabled);
  const setupModules = moduleNames.filter(name => state.modules[name]?.available && !state.modules[name].enabled);
  const availableModules = setupModules.filter(name => !state.modules[name].locked);
  const unavailableModules = moduleNames.filter(name => !state.modules[name]?.available);

  const categoryFor = name => Object.values(moduleCatalog).find(category => category.modules.includes(name));

  const createCard = (name, mode) => {
    const config = state.modules[name];
    const meta = moduleMeta[name];
    const category = categoryFor(name);
    const isEnabled = mode === 'enabled';
    const statusLabel = !config?.available
      ? 'UNAVAILABLE'
      : config.locked
        ? 'LOCKED'
      : isEnabled
        ? state.modulesLive ? 'ONLINE' : 'ENABLED'
        : 'SET UP';
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `module-card ${meta.tone} ${isEnabled ? 'online' : 'setup'}`;
    card.disabled = !config?.available || config.locked;
    card.dataset.module = name;
    card.setAttribute(
      'aria-label',
      `${meta.title}. ${statusLabel}. ${config.locked ? `Requires ${config.required_permission}` : moduleSummary(name, config)}`,
    );
    if (config.locked) card.title = `Requires ${config.required_permission}`;

    const icon = document.createElement('span');
    icon.className = `module-card-icon ${meta.tone}`;
    icon.textContent = meta.icon;
    const details = document.createElement('span');
    details.className = 'module-card-copy';
    const kicker = document.createElement('span');
    kicker.className = 'module-card-kicker';
    kicker.textContent = category?.label || 'Module';
    const label = document.createElement('strong');
    label.textContent = meta.title;
    const description = document.createElement('small');
    description.textContent = meta.copy;
    const summary = document.createElement('span');
    summary.className = 'module-card-summary';
    summary.textContent = config.locked
      ? `Requires ${config.required_permission}`
      : moduleSummary(name, config);
    details.append(kicker, label, description, summary);

    const status = document.createElement('span');
    status.className = 'module-status';
    const statusDot = document.createElement('i');
    status.append(statusDot, document.createTextNode(statusLabel));
    card.append(icon, details, status);
    card.addEventListener('click', () => openModule(name));
    return card;
  };

  const createLane = ({ tone, eyebrow, title, copy, count, modules, emptyTitle, emptyCopy }) => {
    const section = document.createElement('section');
    section.className = `module-lane ${tone}`;
    section.setAttribute('aria-labelledby', `module-lane-${tone}`);
    const heading = document.createElement('div');
    heading.className = 'module-lane-heading';
    const headingCopy = document.createElement('div');
    const eyebrowLabel = document.createElement('span');
    eyebrowLabel.className = 'module-lane-eyebrow';
    eyebrowLabel.textContent = eyebrow;
    const titleElement = document.createElement('h3');
    titleElement.id = `module-lane-${tone}`;
    titleElement.textContent = title;
    const copyElement = document.createElement('p');
    copyElement.textContent = copy;
    headingCopy.append(eyebrowLabel, titleElement, copyElement);
    const countElement = document.createElement('span');
    countElement.className = 'module-lane-count';
    countElement.textContent = count;
    heading.append(headingCopy, countElement);
    section.append(heading);

    if (!modules.length) {
      const empty = document.createElement('div');
      empty.className = 'module-empty-state';
      const emptyIcon = document.createElement('span');
      emptyIcon.textContent = tone === 'enabled' ? '\u25c7' : '\u2726';
      const emptyCopyBlock = document.createElement('span');
      const emptyHeading = document.createElement('strong');
      emptyHeading.textContent = emptyTitle;
      const emptyDescription = document.createElement('small');
      emptyDescription.textContent = emptyCopy;
      emptyCopyBlock.append(emptyHeading, emptyDescription);
      empty.append(emptyIcon, emptyCopyBlock);
      section.append(empty);
      return section;
    }

    const grid = document.createElement('div');
    grid.className = 'module-card-grid';
    for (const name of modules) grid.append(createCard(name, tone));
    section.append(grid);
    return section;
  };

  container.append(createLane({
    tone: 'enabled',
    eyebrow: 'ON NOW',
    title: 'Enabled modules',
    copy: 'These features are currently enabled for this server.',
    count: state.modulesLive ? `${enabledModules.length} ON` : `${enabledModules.length} ENABLED`,
    modules: enabledModules,
    emptyTitle: 'No modules are enabled yet',
    emptyCopy: 'Pick a feature from the section below to get started.',
  }));

  const discovery = createLane({
    tone: 'discovery',
    eyebrow: 'EXPLORE',
    title: 'Add something new',
    copy: 'Choose another feature to set up. Nothing changes until you save.',
    count: `${availableModules.length} READY`,
    modules: setupModules,
    emptyTitle: 'Everything available is already enabled',
    emptyCopy: 'Every available module is already enabled for this server.',
  });
  if (unavailableModules.length) {
    const unavailable = document.createElement('div');
    unavailable.className = 'module-unavailable-note';
    const unavailableIcon = document.createElement('span');
    unavailableIcon.textContent = '!';
    const unavailableCopy = document.createElement('span');
    const unavailableTitle = document.createElement('strong');
    unavailableTitle.textContent = `${unavailableModules.length} temporarily unavailable`;
    const unavailableDescription = document.createElement('small');
    unavailableDescription.textContent = `${unavailableModules.map(name => moduleMeta[name].title).join(', ')} — try again after Fate reconnects.`;
    unavailableCopy.append(unavailableTitle, unavailableDescription);
    unavailable.append(unavailableIcon, unavailableCopy);
    discovery.append(unavailable);
  }
  container.append(discovery);
}

function revealModuleCard(name) {
  requestAnimationFrame(() => {
    const card = byId('module-sections').querySelector(`[data-module="${name}"]`);
    if (!card) return;
    card.classList.add('just-saved');
    card.focus({ preventScroll: true });
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.setTimeout(() => card.classList.remove('just-saved'), 1100);
  });
}

function setEnabled(id, value) {
  byId(id).checked = Boolean(value);
}

function populateChannelSelect(id, value, label = 'Choose a channel') {
  const select = byId(id);
  const applyOptions = () => {
    select.replaceChildren();
    const empty = document.createElement('option');
    empty.value = '__off';
    empty.textContent = label;
    select.append(empty);
    for (const channel of state.channels) {
      const option = document.createElement('option');
      option.value = channel.id;
      option.textContent = `# ${channel.name}`;
      select.append(option);
    }
    if (value && ![...select.options].some(option => option.value === String(value))) {
      const unavailable = document.createElement('option');
      unavailable.value = String(value);
      unavailable.textContent = `Unavailable channel · ${value}`;
      select.append(unavailable);
    }
    select.value = value ? String(value) : '__off';
  };
  applyOptions();
  setTimeout(() => { if (select.isConnected) applyOptions(); }, 0);
}

function populateRoleSelect(id, value, label = 'Choose a role') {
  roleOptions(byId(id), value, label);
}

function addAutoRoleRow(roleId = null, delay = 0) {
  const row = document.createElement('div');
  row.className = 'autorole-row';
  const role = document.createElement('select');
  role.className = 'autorole-role';
  roleOptions(role, roleId, 'Choose a role');
  const timing = document.createElement('div');
  timing.className = 'number-field';
  const input = document.createElement('input');
  input.className = 'autorole-delay';
  input.type = 'number';
  input.min = '0';
  input.max = '31536000';
  input.value = String(delay || 0);
  input.setAttribute('aria-label', 'Delay in seconds');
  const unit = document.createElement('span');
  unit.textContent = 'seconds';
  timing.append(input, unit);
  const remove = document.createElement('button');
  remove.className = 'row-remove';
  remove.type = 'button';
  remove.textContent = '×';
  remove.title = 'Remove this role';
  remove.addEventListener('click', () => row.remove());
  row.append(role, timing, remove);
  byId('autorole-rows').append(row);
}

function addStarboardRow(board = {}) {
  const row = document.createElement('section');
  row.className = 'starboard-row';
  row.dataset.boardId = board.board_id || '';

  const header = document.createElement('div');
  header.className = 'starboard-row-header';
  const nameLabel = document.createElement('label');
  const nameTitle = document.createElement('span');
  nameTitle.textContent = 'Board name';
  const name = document.createElement('input');
  name.className = 'starboard-name';
  name.type = 'text';
  name.maxLength = 40;
  name.required = true;
  name.placeholder = 'Community favorites';
  name.value = board.name || 'Starboard';
  nameLabel.append(nameTitle, name);

  const enabledLabel = document.createElement('label');
  enabledLabel.className = 'starboard-enabled';
  const enabled = document.createElement('input');
  enabled.type = 'checkbox';
  enabled.className = 'starboard-enabled-input';
  enabled.checked = board.enabled !== false;
  const enabledText = document.createElement('span');
  enabledText.textContent = 'Active';
  enabledLabel.append(enabled, enabledText);

  const remove = document.createElement('button');
  remove.className = 'row-remove';
  remove.type = 'button';
  remove.textContent = '×';
  remove.title = 'Remove this starboard';
  remove.setAttribute('aria-label', 'Remove this starboard');
  remove.addEventListener('click', () => row.remove());
  header.append(nameLabel, enabledLabel, remove);

  const fields = document.createElement('div');
  fields.className = 'starboard-row-fields';
  const channelLabel = document.createElement('label');
  channelLabel.innerHTML = '<span>Destination channel</span>';
  const channel = document.createElement('select');
  channel.className = 'starboard-channel';
  channel.required = true;
  channelOptions(channel, board.channel_id, { disabled: true, emptyLabel: 'Choose a channel' });
  channelLabel.append(channel);

  const emojiLabel = document.createElement('label');
  emojiLabel.innerHTML = '<span>Reaction emoji</span>';
  const emoji = document.createElement('input');
  emoji.className = 'starboard-emoji';
  emoji.type = 'text';
  emoji.maxLength = 100;
  emoji.required = true;
  emoji.placeholder = '⭐ or <:spark:123…>';
  emoji.value = board.emoji || '⭐';
  emojiLabel.append(emoji);

  const thresholdLabel = document.createElement('label');
  thresholdLabel.innerHTML = '<span>Reactions needed</span>';
  const threshold = document.createElement('input');
  threshold.className = 'starboard-threshold';
  threshold.type = 'number';
  threshold.min = '1';
  threshold.max = '100';
  threshold.required = true;
  threshold.value = String(board.threshold || 3);
  thresholdLabel.append(threshold);
  fields.append(channelLabel, emojiLabel, thresholdLabel);

  const hint = document.createElement('small');
  hint.className = 'starboard-row-hint';
  hint.textContent = 'Paste any Unicode emoji, or a Discord custom emoji mention. The name can be changed at any time.';
  row.append(header, fields, hint);
  byId('starboard-rows').append(row);
}

function renderStarboardEditor(config, body) {
  body.innerHTML = `
    <section class="starboard-intro"><span aria-hidden="true">✦</span><div><strong>Reaction-powered highlights</strong><small>Create up to 10 independently named boards. Each one can watch a different emoji and post to a different channel.</small></div></section>
    <div class="starboard-rows" id="starboard-rows"></div>
    <button class="inline-add" id="starboard-add" type="button">+ Add starboard</button>
    <p class="dialog-warning">A member cannot promote their own message, and bot reactions are not counted. Removing a board keeps highlights already posted in Discord.</p>`;
  (config.boards || []).forEach(addStarboardRow);
  if (!config.boards?.length) addStarboardRow();
  byId('starboard-add').addEventListener('click', () => {
    if (byId('starboard-rows').children.length >= 10) {
      showToast('A server can have up to 10 starboards.', 'error');
      return;
    }
    addStarboardRow({ name: `Starboard ${byId('starboard-rows').children.length + 1}` });
  });
}

const loggerGroups = [
  { value: 'Mentions', label: 'Role mentions' },
  { value: 'Message Edit', label: 'Edited messages' },
  { value: 'Message Delete', label: 'Deleted messages' },
  { value: 'Message Update', label: 'Pins, reactions, and embeds' },
  { value: 'Server Update', label: 'Server setting changes' },
  { value: 'Channel Update', label: 'Channel and forum changes' },
  { value: 'Role Update', label: 'Role and permission changes' },
  { value: 'Webhook Update', label: 'Webhook changes' },
  { value: 'Associations', label: 'Member joins and removals' },
  { value: 'Member Update', label: 'Member names and roles' },
  { value: 'Emoji Update', label: 'Emoji changes' },
  { value: 'Sticker Update', label: 'Sticker changes' },
  { value: 'Invite Update', label: 'New invites' },
  { value: 'Auto Moderation', label: 'Moderation actions' },
  { value: 'Threads', label: 'Thread changes' },
  { value: 'Ghost Typing', label: 'Hidden typing activity' },
  { value: 'Scheduled Events', label: 'Scheduled event changes' },
  { value: 'Stage Events', label: 'Stage session changes' },
  { value: 'Soundboard', label: 'Soundboard sound changes' },
];

function renderCheckboxGrid(id, options, selected) {
  const container = byId(id);
  const chosen = new Set(selected);
  container.replaceChildren();
  for (const option of options) {
    const value = typeof option === 'string' ? option : option.value;
    const optionLabel = typeof option === 'string' ? option : option.label;
    const label = document.createElement('label');
    label.className = 'check-chip';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = value;
    input.checked = chosen.has(value);
    const span = document.createElement('span');
    span.textContent = optionLabel;
    label.append(input, span);
    container.append(label);
  }
}

function normalizeActivityLogSearch(value) {
  return String(value || '')
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLocaleLowerCase();
}

function activityLogSearchText(entry) {
  const details = entry.details.map(item => `${item.name} ${item.value}`).join(' ');
  return normalizeActivityLogSearch(
    `${entry.title} ${entry.description} ${entry.channel_name} ${details}`,
  );
}

function activityLogSearchTokens(value) {
  return normalizeActivityLogSearch(value).match(/[\p{L}\p{N}_]+/gu) || [];
}

function oneCharacterSpellingDifference(left, right) {
  if (left === right || Math.abs(left.length - right.length) > 1) return false;
  if (left.length === right.length) {
    const differences = [];
    for (let index = 0; index < left.length; index += 1) {
      if (left[index] !== right[index]) differences.push(index);
      if (differences.length > 2) return false;
    }
    if (differences.length === 1) return true;
    return differences.length === 2
      && differences[1] === differences[0] + 1
      && left[differences[0]] === right[differences[1]]
      && left[differences[1]] === right[differences[0]];
  }

  const shorter = left.length < right.length ? left : right;
  const longer = left.length < right.length ? right : left;
  let shortIndex = 0;
  let longIndex = 0;
  let differences = 0;
  while (shortIndex < shorter.length && longIndex < longer.length) {
    if (shorter[shortIndex] === longer[longIndex]) {
      shortIndex += 1;
      longIndex += 1;
      continue;
    }
    differences += 1;
    if (differences > 1) return false;
    longIndex += 1;
  }
  return true;
}

function activityLogCloseSpellingMatch(entry, query) {
  const queryTokens = activityLogSearchTokens(query);
  if (!queryTokens.length || queryTokens.length > 6) return false;
  const candidateTokens = new Set(activityLogSearchTokens(activityLogSearchText(entry)));
  let closeMatches = 0;
  for (const queryToken of queryTokens) {
    if (candidateTokens.has(queryToken)) continue;
    if (
      queryToken.length < 4
      || queryToken.length > 24
      || !/^[a-z0-9_]+$/.test(queryToken)
      || !/[a-z]/.test(queryToken)
    ) return false;
    const close = [...candidateTokens].some(candidate => (
      Math.abs(candidate.length - queryToken.length) <= 1
      && oneCharacterSpellingDifference(queryToken, candidate)
    ));
    if (!close) return false;
    closeMatches += 1;
    if (closeMatches > 1) return false;
  }
  return closeMatches === 1;
}

function activityLogMatches(entry, query, type, timeWindow, searchedQuery) {
  if (type && entry.type !== type) return false;
  if (timeWindow) {
    const age = Date.now() - Date.parse(entry.timestamp);
    if (!Number.isFinite(age) || age > Number(timeWindow)) return false;
  }
  if (!query) return true;
  const visibleText = activityLogSearchText(entry);
  if (visibleText.includes(query)) return true;
  const queryTokens = activityLogSearchTokens(query);
  const visibleTokens = activityLogSearchTokens(visibleText);
  if (
    queryTokens.length
    && queryTokens.every(token => visibleTokens.some(candidate => candidate.startsWith(token)))
  ) return true;
  return entry.source === 'local' && searchedQuery === query;
}

function filterActivityLogs(entries, query, type, timeWindow, searchedQuery) {
  const inScope = entries.filter(entry => (
    activityLogMatches(entry, '', type, timeWindow, searchedQuery)
  ));
  if (!query) return { entries: inScope, closeSpelling: false };
  const exact = inScope.filter(entry => (
    activityLogMatches(entry, query, '', '', searchedQuery)
  ));
  if (exact.length) return { entries: exact, closeSpelling: false };
  const close = inScope.filter(entry => (
    entry.source === 'discord' && activityLogCloseSpellingMatch(entry, query)
  ));
  return { entries: close, closeSpelling: close.length > 0 };
}

function formatActivityTime(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return 'Unknown time';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

function formatStorage(bytes) {
  if (bytes === null || bytes === undefined || bytes === '') return null;
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return null;
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function formatMemoryDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return 'Unavailable';
  const totalMinutes = Math.floor(value / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function setOwnerMemoryText(id, value, fallback = 'Unavailable') {
  byId(id).textContent = value ?? fallback;
}

function setOwnerMemoryMeter(id, percent) {
  const value = Number(percent);
  byId(id).style.width = `${Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0}%`;
}

function renderOwnerMemoryTrend(rssBytes, observedAt) {
  if (Number.isFinite(Number(rssBytes))) {
    state.ownerMemoryHistory.push({ rss: Number(rssBytes), observedAt });
    state.ownerMemoryHistory = state.ownerMemoryHistory.slice(-60);
  }
  const samples = state.ownerMemoryHistory;
  if (!samples.length) return;

  const values = samples.map(sample => sample.rss);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = Math.max(1, high - low);
  const chartWidth = 600;
  const top = 7;
  const chartHeight = 81;
  const points = samples.map((sample, index) => {
    const x = samples.length === 1 ? chartWidth / 2 : index / (samples.length - 1) * chartWidth;
    const y = top + (high - sample.rss) / spread * chartHeight;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  if (points.length === 1) points.unshift(`0,${points[0].split(',')[1]}`);
  if (points.length === 2 && samples.length === 1) points.push(`600,${points[1].split(',')[1]}`);
  byId('owner-memory-trend-line').setAttribute('points', points.join(' '));
  byId('owner-memory-trend-area').setAttribute('points', `0,96 ${points.join(' ')} 600,96`);
  const latest = values.at(-1);
  setOwnerMemoryText(
    'owner-memory-trend-range',
    `${samples.length} sample${samples.length === 1 ? '' : 's'} · ${formatStorage(low)} low · ${formatStorage(latest)} now · ${formatStorage(high)} high`,
  );
}

function renderOwnerMemoryProcesses(process) {
  const container = byId('owner-memory-process-list');
  container.replaceChildren();
  const rows = [];
  if (process?.pid) {
    rows.push({
      name: 'Fate runtime',
      pid: process.pid,
      rss_bytes: process.rss_bytes,
    });
  }
  rows.push(...(Array.isArray(process?.children) ? process.children : []));
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'owner-memory-process-empty';
    empty.textContent = 'Process details are unavailable for this sample.';
    container.append(empty);
  }
  for (const row of rows) {
    const item = document.createElement('div');
    item.className = 'owner-memory-process-row';
    const identity = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = row.name || 'Child process';
    const pid = document.createElement('small');
    pid.textContent = `PID ${row.pid ?? 'unknown'}`;
    const size = document.createElement('span');
    size.textContent = formatStorage(row.rss_bytes) || 'Unavailable';
    identity.append(name, pid);
    item.append(identity, size);
    container.append(item);
  }
  setOwnerMemoryText(
    'owner-memory-process-count',
    `${rows.length} process${rows.length === 1 ? '' : 'es'} · ${formatStorage(process?.tree_rss_bytes) || 'Unavailable'} total`,
  );
}

function renderOwnerMemory(payload) {
  state.ownerMemory = payload;
  const process = payload.process || {};
  const system = payload.system || {};
  const swap = payload.swap || {};
  const python = payload.python || {};
  const cache = payload.message_cache || {};

  setOwnerMemoryText('owner-memory-rss', formatStorage(process.rss_bytes));
  setOwnerMemoryText('owner-memory-tree', formatStorage(process.tree_rss_bytes));
  setOwnerMemoryText('owner-memory-process-percent', `${Number(process.percent || 0).toFixed(2)}%`);
  setOwnerMemoryMeter('owner-memory-process-meter', process.percent);

  setOwnerMemoryText('owner-memory-system-used', formatStorage(system.used_bytes));
  setOwnerMemoryText('owner-memory-system-total', formatStorage(system.total_bytes));
  setOwnerMemoryText('owner-memory-available', formatStorage(system.available_bytes));
  setOwnerMemoryText('owner-memory-pressure', `${system.pressure || 'unknown'} pressure`);
  setOwnerMemoryMeter('owner-memory-system-meter', system.used_percent);

  setOwnerMemoryText('owner-memory-swap-used', formatStorage(swap.used_bytes));
  setOwnerMemoryText('owner-memory-swap-total', formatStorage(swap.total_bytes));
  setOwnerMemoryText('owner-memory-swap-percent', `${Number(swap.used_percent || 0).toFixed(1)}%`);
  setOwnerMemoryMeter('owner-memory-swap-meter', swap.used_percent);

  if (cache.available) {
    setOwnerMemoryText('owner-memory-cache-current', Number(cache.current || 0).toLocaleString());
    setOwnerMemoryText('owner-memory-cache-capacity', cache.capacity == null ? 'unbounded' : Number(cache.capacity).toLocaleString());
    setOwnerMemoryText('owner-memory-cache-note', cache.fill_percent == null ? 'Cache capacity is not capped' : `${Number(cache.fill_percent).toFixed(1)}% of cache slots occupied`);
    setOwnerMemoryMeter('owner-memory-cache-meter', cache.fill_percent);
  } else {
    setOwnerMemoryText('owner-memory-cache-current', 'Offline');
    setOwnerMemoryText('owner-memory-cache-capacity', '—');
    setOwnerMemoryText('owner-memory-cache-note', 'Available when Dashboard runs inside Fate');
    setOwnerMemoryMeter('owner-memory-cache-meter', 0);
  }

  setOwnerMemoryText('owner-memory-unique', formatStorage(process.uss_bytes ?? process.private_bytes));
  setOwnerMemoryText('owner-memory-vms', formatStorage(process.vms_bytes));
  setOwnerMemoryText('owner-memory-peak', formatStorage(process.peak_rss_bytes));
  setOwnerMemoryText('owner-memory-children', Number(process.child_count || 0).toLocaleString());
  setOwnerMemoryText('owner-memory-uptime', formatMemoryDuration(process.uptime_seconds));

  setOwnerMemoryText('owner-memory-objects', Number(python.tracked_objects || 0).toLocaleString());
  setOwnerMemoryText('owner-memory-blocks', python.allocated_blocks == null ? 'Unavailable' : Number(python.allocated_blocks).toLocaleString());
  setOwnerMemoryText('owner-memory-generations', Array.isArray(python.generation_counts) ? python.generation_counts.map(Number).join(' / ') : null);
  setOwnerMemoryText('owner-memory-collections', Array.isArray(python.collections) ? python.collections.map(Number).join(' / ') : null);
  setOwnerMemoryText('owner-memory-uncollectable', Number(python.uncollectable_objects || 0).toLocaleString());

  setOwnerMemoryText('owner-memory-host-free', formatStorage(system.free_bytes));
  setOwnerMemoryText('owner-memory-host-utilization', `${Number(system.used_percent || 0).toFixed(1)}%`);
  setOwnerMemoryText('owner-memory-host-process-share', `${Number(process.percent || 0).toFixed(2)}%`);
  setOwnerMemoryText('owner-memory-swap-free', formatStorage(swap.free_bytes));
  const pagedIn = formatStorage(swap.paged_in_bytes);
  const pagedOut = formatStorage(swap.paged_out_bytes);
  setOwnerMemoryText('owner-memory-paging', pagedIn || pagedOut ? `${pagedIn || '—'} / ${pagedOut || '—'}` : null);

  renderOwnerMemoryTrend(process.rss_bytes, payload.observed_at);
  renderOwnerMemoryProcesses(process);
  const observed = new Date(payload.observed_at);
  const timestamp = Number.isNaN(observed.getTime()) ? 'just now' : observed.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const status = byId('owner-memory-status');
  status.className = '';
  const liveDot = document.createElement('i');
  liveDot.className = 'live-dot';
  status.replaceChildren(liveDot);
  status.append(document.createTextNode(`Updated ${timestamp}`));
}

function stopOwnerMemoryRefresh() {
  if (ownerMemoryTimer) window.clearInterval(ownerMemoryTimer);
  ownerMemoryTimer = 0;
}

function startOwnerMemoryRefresh() {
  stopOwnerMemoryRefresh();
  ownerMemoryTimer = window.setInterval(() => {
    if (byId('owner-panel').classList.contains('active')) loadOwnerMemory();
  }, 10000);
}

async function loadOwnerMemory({ feedback = false } = {}) {
  if (!state.session?.is_owner || state.ownerMemoryLoading) return;
  state.ownerMemoryLoading = true;
  const refreshButton = byId('owner-memory-refresh');
  const status = byId('owner-memory-status');
  refreshButton.disabled = true;
  status.className = '';
  status.textContent = 'Reading memory telemetry…';
  try {
    renderOwnerMemory(await api('/api/owner/memory'));
    if (feedback) showToast('Memory telemetry refreshed.');
  } catch (error) {
    if ([401, 403].includes(error.status)) {
      clearSensitiveDashboardState();
      location.reload();
      return;
    }
    status.className = 'error';
    status.textContent = error.message;
    if (feedback) showToast(error.message, true);
  } finally {
    state.ownerMemoryLoading = false;
    refreshButton.disabled = false;
  }
}

function formatArchiveUsage(archive) {
  const storage = formatStorage(archive?.stored_bytes);
  if (!storage) return 'Current storage use is temporarily unavailable.';
  const records = Number(archive?.log_count || 0).toLocaleString();
  return `${storage} retained · ${records} saved records`;
}

function appendDiscordCustomEmoji(container, animated, name, emojiId) {
  const emoji = document.createElement('img');
  const fallback = `:${name}:`;
  const animatedQuery = animated ? '&animated=true' : '';
  emoji.className = 'discord-custom-emoji';
  emoji.src = `https://cdn.discordapp.com/emojis/${emojiId}.webp?size=32${animatedQuery}&quality=lossless`;
  emoji.alt = fallback;
  emoji.title = fallback;
  emoji.loading = 'lazy';
  emoji.decoding = 'async';
  emoji.draggable = false;
  emoji.addEventListener('error', () => {
    emoji.replaceWith(document.createTextNode(fallback));
  }, { once: true });
  container.append(emoji);
}

function formatDiscordRelativeTime(date) {
  const seconds = (date.getTime() - Date.now()) / 1000;
  const absolute = Math.abs(seconds);
  let unit = 'second';
  let divisor = 1;
  if (absolute >= 365 * 24 * 60 * 60) {
    unit = 'year';
    divisor = 365 * 24 * 60 * 60;
  } else if (absolute >= 30 * 24 * 60 * 60) {
    unit = 'month';
    divisor = 30 * 24 * 60 * 60;
  } else if (absolute >= 24 * 60 * 60) {
    unit = 'day';
    divisor = 24 * 60 * 60;
  } else if (absolute >= 60 * 60) {
    unit = 'hour';
    divisor = 60 * 60;
  } else if (absolute >= 60) {
    unit = 'minute';
    divisor = 60;
  }
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
    .format(Math.round(seconds / divisor), unit);
}

function formatDiscordTimestamp(date, style = 'f') {
  if (style === 'R') return formatDiscordRelativeTime(date);
  const options = {
    t: { hour: 'numeric', minute: '2-digit' },
    T: { hour: 'numeric', minute: '2-digit', second: '2-digit' },
    d: { year: 'numeric', month: '2-digit', day: '2-digit' },
    D: { year: 'numeric', month: 'long', day: 'numeric' },
    f: { year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' },
    F: { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' },
    s: { year: 'numeric', month: '2-digit', day: '2-digit', hour: 'numeric', minute: '2-digit' },
    S: { year: 'numeric', month: '2-digit', day: '2-digit', hour: 'numeric', minute: '2-digit', second: '2-digit' },
  };
  return new Intl.DateTimeFormat(undefined, options[style] || options.f).format(date);
}

function appendDiscordTimestamp(container, raw, unixSeconds, style) {
  const seconds = Number(unixSeconds);
  const date = new Date(seconds * 1000);
  if (!Number.isSafeInteger(seconds) || Number.isNaN(date.getTime())) {
    container.append(document.createTextNode(raw));
    return;
  }
  const timestamp = document.createElement('time');
  timestamp.className = 'discord-timestamp';
  timestamp.dateTime = date.toISOString();
  timestamp.dataset.style = style || 'f';
  timestamp.textContent = formatDiscordTimestamp(date, style || 'f');
  timestamp.title = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'full',
    timeStyle: 'long',
  }).format(date);
  container.append(timestamp);
}

function appendDiscordEmojiText(container, content) {
  const discordTokenPattern = /<(a?):([A-Za-z0-9_]{2,32}):(\d{17,20})>|<t:(-?\d{1,13})(?::([tTdDfFsSR]))?>/g;
  const text = String(content || '');
  let cursor = 0;
  let match;
  while ((match = discordTokenPattern.exec(text)) !== null) {
    if (match.index > cursor) {
      container.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    if (match[2]) {
      appendDiscordCustomEmoji(container, Boolean(match[1]), match[2], match[3]);
    } else {
      appendDiscordTimestamp(container, match[0], match[4], match[5]);
    }
    cursor = discordTokenPattern.lastIndex;
  }
  if (cursor < text.length) {
    container.append(document.createTextNode(text.slice(cursor)));
  }
}

function appendDiscordInline(container, content) {
  const tokenPattern = /<(a?):([A-Za-z0-9_]{2,32}):(\d{17,20})>|<t:(-?\d{1,13})(?::([tTdDfFsSR]))?>|\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]+)\)|\*\*([^*\n]+)\*\*|`([^`\n]+)`/g;
  let cursor = 0;
  let match;
  while ((match = tokenPattern.exec(content)) !== null) {
    if (match.index > cursor) container.append(document.createTextNode(content.slice(cursor, match.index)));
    if (match[2]) {
      appendDiscordCustomEmoji(container, Boolean(match[1]), match[2], match[3]);
    } else if (match[4]) {
      appendDiscordTimestamp(container, match[0], match[4], match[5]);
    } else if (match[6]) {
      const link = document.createElement('a');
      link.href = match[7];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      appendDiscordEmojiText(link, match[6]);
      container.append(link);
    } else if (match[8]) {
      const strong = document.createElement('strong');
      appendDiscordEmojiText(strong, match[8]);
      container.append(strong);
    } else {
      const code = document.createElement('code');
      code.textContent = match[9];
      container.append(code);
    }
    cursor = tokenPattern.lastIndex;
  }
  if (cursor < content.length) container.append(document.createTextNode(content.slice(cursor)));
}

function renderDiscordText(container, content) {
  container.replaceChildren();
  const lines = String(content || '').replace(/\r\n/g, '\n').split('\n');
  lines.forEach(line => {
    const row = document.createElement('span');
    row.className = 'discord-log-line';
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) row.classList.add('quote');
    appendDiscordInline(row, quote ? quote[1] : line);
    if (!row.childNodes.length) row.append(document.createElement('br'));
    container.append(row);
  });
}

function renderActivityLogs() {
  if (state.editingModule !== 'logger') return;
  const list = byId('activity-log-results');
  const search = byId('activity-log-search');
  const typeSelect = byId('activity-log-type');
  const timeSelect = byId('activity-log-time');
  if (!list || !search || !typeSelect || !timeSelect) return;

  const previousType = typeSelect.value;
  const types = [...new Set(state.activityLogs.entries.map(entry => entry.type))]
    .sort((left, right) => left.localeCompare(right));
  typeSelect.replaceChildren(new Option('All activity types', ''));
  types.forEach(type => typeSelect.add(new Option(type, type)));
  typeSelect.value = types.includes(previousType) ? previousType : '';

  const query = normalizeActivityLogSearch(search.value.trim());
  const filtered = filterActivityLogs(
    state.activityLogs.entries,
    query,
    typeSelect.value,
    timeSelect.value,
    state.activityLogs.searchedQuery,
  );
  const visible = filtered.entries;
  list.replaceChildren();

  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'activity-log-empty';
    const icon = document.createElement('span');
    icon.textContent = state.activityLogs.entries.length ? '⌕' : '≡';
    const copy = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = state.activityLogs.entries.length ? 'No matching activity' : 'No activity to show yet';
    const detail = document.createElement('small');
    detail.textContent = state.activityLogs.entries.length
      ? 'Try a broader search, another activity type, or a longer time window.'
      : (state.activityLogs.reason || 'New Fate activity will appear here automatically.');
    copy.append(title, detail);
    empty.append(icon, copy);
    list.append(empty);
  } else {
    for (const entry of visible) {
      const card = document.createElement('article');
      card.className = 'activity-log-entry';
      if (Number.isInteger(entry.color)) {
        card.style.setProperty('--entry-color', `#${entry.color.toString(16).padStart(6, '0')}`);
      }

      const header = document.createElement('header');
      const heading = document.createElement('div');
      heading.className = 'activity-log-entry-heading';
      const marker = document.createElement('i');
      const title = document.createElement('strong');
      appendDiscordEmojiText(title, entry.title);
      heading.append(marker, title);
      const when = document.createElement('time');
      when.dateTime = entry.timestamp;
      when.textContent = formatActivityTime(entry.timestamp);
      header.append(heading, when);
      card.append(header);

      if (entry.description) {
        const description = document.createElement('div');
        description.className = 'activity-log-description';
        renderDiscordText(description, entry.description);
        card.append(description);
      }

      if (entry.details.length) {
        const details = document.createElement('dl');
        for (const item of entry.details.slice(0, 8)) {
          const row = document.createElement('div');
          const name = document.createElement('dt');
          appendDiscordEmojiText(name, item.name);
          const value = document.createElement('dd');
          renderDiscordText(value, item.value);
          row.append(name, value);
          details.append(row);
        }
        card.append(details);
      }

      const footer = document.createElement('footer');
      const channel = document.createElement('span');
      channel.textContent = `# ${entry.channel_name}`;
      footer.append(channel);
      if (entry.jump_url) {
        const jump = document.createElement('a');
        jump.href = entry.jump_url;
        jump.target = '_blank';
        jump.rel = 'noopener noreferrer';
        jump.textContent = 'Open in Discord ↗';
        footer.append(jump);
      } else if (entry.source === 'local') {
        const saved = document.createElement('span');
        saved.className = 'activity-log-saved-badge';
        saved.textContent = 'Saved locally';
        footer.append(saved);
      }
      card.append(footer);
      list.append(card);
    }
  }

  const count = byId('activity-log-count');
  const storage = formatStorage(state.activityLogs.archive?.stored_bytes);
  const closeMatchNote = filtered.closeSpelling
    || (
      state.activityLogs.searchedQuery === query
      && ['fuzzy', 'close'].includes(state.activityLogs.matchMode)
    )
    ? ' · no exact match, showing close spellings'
    : '';
  count.textContent = `Showing ${visible.length} of ${state.activityLogs.entries.length} loaded${closeMatchNote}${storage ? ` · ${storage} retained` : ''}`;
  const archiveUsage = byId('logger-archive-usage');
  if (archiveUsage && state.activityLogs.archive?.enabled) {
    archiveUsage.textContent = formatArchiveUsage(state.activityLogs.archive);
  }
  const warning = byId('activity-log-warning');
  warning.hidden = !state.activityLogs.warning;
  warning.textContent = state.activityLogs.warning || '';
  const loadMore = byId('activity-log-more');
  loadMore.hidden = !state.activityLogs.hasMore;
  loadMore.disabled = false;
}

async function loadActivityLogs({ reset = false } = {}) {
  if (state.editingModule !== 'logger' || !state.guildId) return;
  const requestId = ++activityLogRequest;
  const refresh = byId('activity-log-refresh');
  const loadMore = byId('activity-log-more');
  refresh.disabled = true;
  loadMore.disabled = true;
  byId('activity-log-count').textContent = reset ? 'Loading latest activity…' : 'Loading older activity…';
  if (reset) {
    const currentArchive = state.activityLogs.archive;
    state.activityLogs = emptyActivityLogState(currentArchive);
    byId('activity-log-results').innerHTML = '<div class="activity-log-loading"><span></span><strong>Reading the latest server activity</strong></div>';
  }

  const query = new URLSearchParams({ limit: '50' });
  if (!reset && state.activityLogs.nextBefore) query.set('before', state.activityLogs.nextBefore);
  const savedSearch = byId('activity-log-search')?.value.trim();
  const serverSearch = state.modules.logger?.local_archive?.enabled && savedSearch
    ? savedSearch
    : null;
  if (serverSearch) query.set('q', serverSearch);
  try {
    const payload = await api(`/api/guilds/${state.guildId}/activity-logs?${query}`);
    if (requestId !== activityLogRequest || state.editingModule !== 'logger') return;
    const existing = reset ? [] : state.activityLogs.entries;
    const known = new Set(existing.map(entry => entry.id));
    state.activityLogs = {
      entries: [...existing, ...payload.entries.filter(entry => !known.has(entry.id))],
      nextBefore: payload.next_before,
      hasMore: payload.has_more,
      reason: payload.reason,
      warning: payload.warning,
      source: payload.source,
      archive: payload.archive ?? (
        state.modules.logger?.local_archive?.enabled
          ? { enabled: true, stored_bytes: null }
          : null
      ),
      matchMode: payload.match_mode
        ?? payload.entries[0]?.search_match
        ?? (reset ? null : state.activityLogs.matchMode),
      searchedQuery: serverSearch ? normalizeActivityLogSearch(serverSearch) : null,
    };
    renderActivityLogs();
  } catch (error) {
    if (requestId !== activityLogRequest || state.editingModule !== 'logger') return;
    state.activityLogs.reason = error.message;
    state.activityLogs.warning = null;
    state.activityLogs.hasMore = false;
    if (!state.activityLogs.archive && state.modules.logger?.local_archive?.enabled) {
      state.activityLogs.archive = { enabled: true, stored_bytes: null };
    }
    renderActivityLogs();
  } finally {
    if (requestId === activityLogRequest) {
      refresh.disabled = false;
      loadMore.disabled = !state.activityLogs.hasMore;
    }
  }
}

function setupActivityLogBrowser() {
  const search = byId('activity-log-search');
  search.addEventListener('input', () => {
    renderActivityLogs();
    window.clearTimeout(activityLogSearchTimer);
    if (state.modules.logger?.local_archive?.enabled) {
      activityLogSearchTimer = window.setTimeout(() => loadActivityLogs({ reset: true }), 350);
    }
  });
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter') event.preventDefault();
  });
  byId('activity-log-type').addEventListener('change', renderActivityLogs);
  byId('activity-log-time').addEventListener('change', renderActivityLogs);
  byId('activity-log-refresh').addEventListener('click', () => loadActivityLogs({ reset: true }));
  byId('activity-log-more').addEventListener('click', () => loadActivityLogs());
}

function updateLoggerJsonStatus() {
  const editor = byId('logger-json');
  const status = byId('logger-json-status');
  if (!editor || !status) return;
  if (state.loggerEditorSource !== 'json') {
    status.className = 'logging-json-status';
    status.textContent = 'Current configuration loaded. Edit the JSON to save from this advanced view.';
    return;
  }
  try {
    JSON.parse(editor.value);
    status.className = 'logging-json-status valid';
    status.textContent = 'Valid JSON — these values will be saved when you choose Save settings.';
  } catch (error) {
    status.className = 'logging-json-status invalid';
    status.textContent = `Fix the JSON before saving: ${error.message}`;
  }
}

function useLoggerControls() {
  state.loggerEditorSource = 'controls';
  updateLoggerJsonStatus();
}

function syncLoggerArchiveUi() {
  const enabled = byId('logger-archive-enabled')?.checked;
  const attachmentsEnabled = enabled && byId('logger-archive-attachments')?.checked;
  const fields = byId('logger-archive-fields');
  if (!fields) return;
  fields.classList.toggle('is-disabled', !enabled);
  for (const control of fields.querySelectorAll('input')) control.disabled = !enabled;
  const fileLimit = byId('logger-archive-file-limit');
  const fileLimitSetting = fileLimit?.closest('.logger-archive-file-limit');
  if (fileLimit) fileLimit.disabled = !attachmentsEnabled;
  fileLimitSetting?.classList.toggle('is-disabled', !attachmentsEnabled);
  const usage = byId('logger-archive-usage');
  if (usage) {
    const savedEnabled = state.modules.logger?.local_archive?.enabled === true;
    usage.textContent = enabled
      ? (
        savedEnabled
          ? formatArchiveUsage(state.activityLogs.archive)
          : 'Save settings to start local history.'
      )
      : (savedEnabled ? 'Save settings to stop saving new history.' : 'Off by default · no server history is stored');
  }
}

function renderModmailEmpty(titleText, detailText, iconText = '✉') {
  const list = byId('modmail-entry-list');
  if (!list) return;
  list.replaceChildren();
  const empty = document.createElement('div');
  empty.className = 'modmail-entry-empty';
  const icon = document.createElement('span');
  icon.textContent = iconText;
  const copy = document.createElement('div');
  const title = document.createElement('strong');
  title.textContent = titleText;
  const detail = document.createElement('small');
  detail.textContent = detailText;
  copy.append(title, detail);
  empty.append(icon, copy);
  list.append(empty);
}

function renderModmailEntries(payload) {
  if (state.editingModule !== 'modmail') return;
  const list = byId('modmail-entry-list');
  const summary = byId('modmail-entry-summary');
  if (!list || !summary) return;

  if (!payload.enabled) {
    summary.textContent = 'Conversation previews appear after Modmail is enabled.';
    renderModmailEmpty('Modmail is not enabled yet', 'Choose a staff channel, turn Modmail on, and save your settings.');
    return;
  }
  if (!payload.entries.length) {
    summary.textContent = payload.channel_name
      ? `Watching #${payload.channel_name}`
      : 'The saved staff channel could not be opened.';
    renderModmailEmpty(
      'No Modmail conversations yet',
      payload.warning || 'New and archived Discord conversations will appear here automatically.',
      '◇',
    );
    return;
  }

  const channelLabel = payload.channel_name ? ` in #${payload.channel_name}` : '';
  summary.textContent = `${payload.entries.length} latest conversation${payload.entries.length === 1 ? '' : 's'}${channelLabel}`;
  list.replaceChildren();
  for (const entry of payload.entries) {
    const card = document.createElement('article');
    card.className = `modmail-entry modmail-entry-${entry.status}`;

    const header = document.createElement('header');
    const heading = document.createElement('div');
    const marker = document.createElement('i');
    const title = document.createElement('strong');
    title.textContent = entry.case_number ? `Case ${entry.case_number}` : entry.name;
    heading.append(marker, title);
    const status = document.createElement('span');
    status.className = `modmail-entry-status ${entry.status}`;
    status.textContent = entry.status === 'active' ? 'Active' : entry.status === 'closed' ? 'Closed' : 'Archived';
    header.append(heading, status);
    card.append(header);

    const threadName = document.createElement('small');
    threadName.className = 'modmail-entry-name';
    threadName.textContent = entry.name;
    card.append(threadName);

    const message = document.createElement('p');
    message.textContent = entry.latest_message || 'No message preview is available for this conversation.';
    card.append(message);

    const footer = document.createElement('footer');
    const metadata = document.createElement('span');
    const author = entry.latest_author ? `Latest from ${entry.latest_author}` : 'Latest activity';
    const count = Number.isInteger(entry.message_count) ? ` · ${entry.message_count} messages` : '';
    const when = entry.updated_at ? ` · ${formatActivityTime(entry.updated_at)}` : '';
    metadata.textContent = `${author}${count}${when}`;
    const jump = document.createElement('a');
    jump.href = entry.url;
    jump.target = '_blank';
    jump.rel = 'noopener noreferrer';
    jump.textContent = 'Open in Discord ↗';
    footer.append(metadata, jump);
    card.append(footer);
    list.append(card);
  }
}

async function loadModmailEntries() {
  if (state.editingModule !== 'modmail' || !state.guildId) return;
  const requestId = ++modmailEntryRequest;
  const refresh = byId('modmail-entry-refresh');
  if (!refresh) return;
  refresh.disabled = true;
  byId('modmail-entry-summary').textContent = 'Reading Discord conversations…';
  byId('modmail-entry-list').innerHTML = '<div class="modmail-entry-loading"><span></span><strong>Reading Modmail conversations</strong></div>';
  try {
    const payload = await api(`/api/guilds/${state.guildId}/modules/modmail/entries`);
    if (requestId !== modmailEntryRequest || state.editingModule !== 'modmail') return;
    renderModmailEntries(payload);
  } catch (error) {
    if (requestId !== modmailEntryRequest || state.editingModule !== 'modmail') return;
    byId('modmail-entry-summary').textContent = 'Conversation preview unavailable';
    renderModmailEmpty('Could not load Modmail conversations', error.message, '!');
  } finally {
    if (requestId === modmailEntryRequest && refresh.isConnected) refresh.disabled = false;
  }
}

function renderMessageModule(config, name) {
  const isWelcome = name === 'welcome';
  const label = isWelcome ? 'Welcome' : 'Goodbye';
  byId('module-dialog-body').innerHTML = `
    ${toggleSetting('mod-enabled', `Enable ${label.toLowerCase()} messages`, `Post automatically when a member ${isWelcome ? 'joins' : 'leaves'}.`)}
    ${selectSetting('mod-channel', `${label} channel`, `Choose where Fate should post ${label.toLowerCase()} messages.`)}
    ${textSetting('mod-message', 'Message', isWelcome ? 'Available placeholders: !mention, !user, !name, !server, !count, and !inviter.' : 'Available placeholders: !mention, !user, and !server.')}
    ${toggleSetting('mod-use-images', 'Add an image', 'Use your own image links or let Fate choose an image.')}
    ${textSetting('mod-images', 'Your image links', 'Add one complete image link per line. Leave this empty to use Fate’s images.', 'https://example.com/image.gif')}
    ${isWelcome ? toggleSetting('mod-wait-verify', 'Wait for verification', 'Send the welcome message only after the member passes verification.') : ''}`;
  setEnabled('mod-enabled', config.enabled);
  populateChannelSelect('mod-channel', config.channel_id);
  byId('mod-message').value = config.message;
  setEnabled('mod-use-images', config.use_images);
  byId('mod-images').value = config.image_urls.join('\n');
  if (isWelcome) setEnabled('mod-wait-verify', config.wait_for_verify);
}

function renderSelfRoleEditor(menuId = '__new') {
  const config = state.modules.selfroles;
  const body = byId('module-dialog-body');
  const menu = config.menus.find(item => item.menu_id === menuId);
  body.innerHTML = `
    <label class="dialog-setting dialog-setting-stack"><span><strong>Role menu</strong><small>Choose an existing menu to edit or create a new one.</small></span><select id="selfrole-menu-picker"></select></label>
    <div id="selfrole-editor">
      ${selectSetting('mod-channel', 'Channel', 'Choose where a new role menu should be posted. Existing menus stay where they are.')}
      ${textSetting('mod-message', 'Menu message', 'The text shown above the role picker.')}
      <label class="dialog-setting dialog-setting-stack"><span><strong>Roles</strong><small>Members can choose from up to 25 roles. Click each role to add or remove it.</small></span><select id="mod-roles" multiple size="6"></select></label>
      <div class="dialog-split">
        <label class="dialog-setting dialog-setting-stack"><span><strong>Menu style</strong><small>Choose a dropdown list or a row of buttons.</small></span><select id="mod-style"><option value="dropdown">Dropdown</option><option value="buttons">Buttons</option></select></label>
        <label class="dialog-setting dialog-setting-stack"><span><strong>Maximum choices</strong><small>Use 0 to let members choose any number of roles.</small></span><input id="mod-limit" type="number" min="0" max="25"></label>
      </div>
      ${toggleSetting('mod-show-roles', 'Show role mentions', 'List the roles beneath the menu message.')}
      ${toggleSetting('mod-show-percentage', 'Show member percentages', 'Show how popular each role is.')}
    </div>
    <p class="dialog-warning" id="selfrole-warning" hidden></p>`;

  const picker = byId('selfrole-menu-picker');
  picker.add(new Option('Create a new menu', '__new'));
  config.menus.forEach((item, index) => picker.add(new Option(`${item.text.split('\n')[0].slice(0, 45)} · Menu ${index + 1}`, item.menu_id)));
  picker.value = menu?.menu_id || '__new';
  picker.addEventListener('change', event => renderSelfRoleEditor(event.target.value));

  const current = menu || {
    channel_id: null,
    role_ids: [],
    text: 'Choose your role',
    style: 'dropdown',
    limit: 1,
    show_roles: true,
    show_percentage: true,
    editable: true,
  };
  populateChannelSelect('mod-channel', current.channel_id);
  byId('mod-channel').disabled = Boolean(menu);
  byId('mod-message').value = current.text;
  setSelectOptions(byId('mod-roles'), state.roles, current.role_ids, '@ ');
  byId('mod-style').value = current.style === 'buttons' ? 'buttons' : 'dropdown';
  byId('mod-limit').value = String(current.limit || 0);
  setEnabled('mod-show-roles', current.show_roles);
  setEnabled('mod-show-percentage', current.show_percentage);
  const unsupported = Boolean(menu && !menu.editable);
  byId('selfrole-editor').classList.toggle('is-disabled', unsupported);
  byId('module-save').disabled = unsupported;
  byId('module-delete').hidden = !menu || unsupported;
  if (unsupported) {
    byId('selfrole-warning').hidden = false;
    byId('selfrole-warning').textContent = 'This menu uses categories and can only be edited in Discord.';
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function antiSpamRulesText(rules = []) {
  return rules.map(rule => `${rule.threshold} msgs per ${rule.timespan} sec`).join('\n');
}

function parseAntiSpamRules(value, label) {
  const rules = [];
  const seen = new Set();
  for (const [index, line] of value.split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    const match = line.trim().match(/^(\d+)\s*(?:(?:messages?|msgs?)\s*)?(?:\/|,|in|per)\s*(\d+)\s*(?:seconds?|secs?|s)?$/i);
    if (!match) throw new Error(`${label} line ${index + 1} must look like “4 msgs per 3 sec”.`);
    const threshold = Number(match[1]);
    const timespan = Number(match[2]);
    if (threshold < 2 || threshold > 100) throw new Error(`${label} message counts must be between 2 and 100.`);
    if (timespan < 1 || timespan > 3600) throw new Error(`${label} windows must be between 1 and 3,600 seconds.`);
    const marker = `${threshold}/${timespan}`;
    if (!seen.has(marker)) rules.push({ threshold, timespan });
    seen.add(marker);
  }
  if (rules.length > 25) throw new Error(`${label} supports up to 25 rolling rules.`);
  return rules;
}

function antiSpamNumber(id, title, copy, value, min, max, unit = '') {
  return `<label class="antispam-field"><span><strong>${title}</strong><small>${copy}</small></span><span class="antispam-number"><input id="${id}" type="number" min="${min}" max="${max}" step="1" value="${value}" required>${unit ? `<em>${unit}</em>` : ''}</span></label>`;
}

function antiSpamRelation(title, copy, first, connector, second, ending) {
  const input = field => `<span class="antispam-number"><input id="${field.id}" aria-label="${title} ${field.unit}" type="number" min="${field.min}" max="${field.max}" step="1" value="${field.value}" required><em>${field.unit}</em></span>`;
  return `<label class="antispam-field antispam-combined-field"><span><strong>${title}</strong><small>${copy}</small></span><span class="antispam-relation">${input(first)}<b>${connector}</b>${input(second)}<b>${ending}</b></span></label>`;
}

function antiSpamActionOptions(target, action) {
  const knownTimeouts = new Set(['timeout:60', 'timeout:300', 'timeout:600', 'timeout:3600', 'timeout:86400']);
  const selected = action || (target === 'default' ? 'adaptive' : 'inherit');
  const options = [
    ...(target === 'default' ? [] : [['inherit', 'Use default response']]),
    ['observe', 'Observe only'], ['delete', 'Delete only'], ['warn', 'Warn + delete'],
    ['adaptive', 'Adaptive timeout'], ['timeout:60', 'Timeout · 1 minute'],
    ['timeout:300', 'Timeout · 5 minutes'], ['timeout:600', 'Timeout · 10 minutes'],
    ['timeout:3600', 'Timeout · 1 hour'], ['timeout:86400', 'Timeout · 24 hours'],
    ['custom', 'Custom timeout…'], ['kick', 'Kick'], ['ban', 'Ban'],
  ];
  const selectedValue = selected.startsWith('timeout:') && !knownTimeouts.has(selected) ? 'custom' : selected;
  return options.map(([value, label]) => `<option value="${value}"${value === selectedValue ? ' selected' : ''}>${label}</option>`).join('');
}

function antiSpamPolicyRows(config) {
  return antiSpamPolicies.map(([target, label, copy]) => {
    const action = config.punishments?.[target] || (target === 'default' ? 'adaptive' : 'inherit');
    const customSeconds = action.startsWith('timeout:') ? Number(action.split(':')[1]) : 600;
    const custom = action.startsWith('timeout:') && !['timeout:60', 'timeout:300', 'timeout:600', 'timeout:3600', 'timeout:86400'].includes(action);
    return `<div class="antispam-policy-row"><span><strong>${label}</strong><small>${copy}</small></span><select data-antispam-policy="${target}">${antiSpamActionOptions(target, action)}</select><label class="antispam-custom-timeout"${custom ? '' : ' hidden'}><input type="number" min="10" max="2419200" value="${customSeconds}"><span>seconds</span></label></div>`;
  }).join('');
}

function antiSpamProtectionCards(config) {
  const protection = key => config.protections?.[key] || {};
  const card = (key, body, open = false) => {
    const info = antiSpamModules.find(([name]) => name === key);
    return `<details class="antispam-protection"${open ? ' open' : ''}><summary><span><strong>${info[1]}</strong><small>${info[2]}</small></span><span class="antispam-state" data-module-state="${key}">${protection(key).enabled ? 'Enabled' : 'Disabled'}</span></summary><div class="antispam-protection-body">${toggleSetting(`asp-${key}-enabled`, `Enable ${info[1]}`, 'Disable this module without losing its tuned settings.')}${body}</div></details>`;
  };
  return [
    card('rate_limit', textSetting('asp-rate-rules', 'Rolling flood limits', 'One readable “messages per seconds” rule per line. A member triggers when any limit is reached.', '4 msgs per 3 sec\n6 msgs per 10 sec'), true),
    card('mass_pings', `<div class="antispam-field-grid">${antiSpamNumber('asp-pings-per-message', 'Mention weight per message', 'Users count as 1; role and everyone mentions carry more weight. Set 0 to disable.', protection('mass_pings').per_message, 0, 100, 'weight')}${toggleSetting('asp-pings-ghost', 'Detect ghost pings', 'Record mention messages deleted by their author and notify staff.')}</div>${textSetting('asp-pings-rules', 'Rolling mention limits', 'Counts mention-bearing messages per member in each window.', '3 msgs per 10 sec\n6 msgs per 30 sec')}`),
    card('duplicates', `<div class="antispam-field-grid">${antiSpamNumber('asp-duplicate-segments', 'Repeated-word limit', 'Repeated normalized segments in one message. Set 0 to disable.', protection('duplicates').per_message, 0, 100, 'repeats')}${antiSpamNumber('asp-link-window', 'Same-link window', 'Canonical destination matching ignores common trackers.', protection('duplicates').same_link, 0, 3600, 'sec')}${antiSpamNumber('asp-image-window', 'Same-image window', 'Perceptual image matching catches resized copies.', protection('duplicates').same_image, 0, 3600, 'sec')}${antiSpamNumber('asp-sticker-window', 'Sticker burst window', 'Rapid sticker volume window. Set 0 to disable.', protection('duplicates').sticker, 0, 3600, 'sec')}${antiSpamNumber('asp-same-sticker-window', 'Same-sticker window', 'Repeated identical sticker window. Set 0 to disable.', protection('duplicates').same_sticker, 0, 3600, 'sec')}${antiSpamNumber('asp-thread-limit', 'Open threads per member', 'Maximum open threads or forum posts. Set 0 to disable.', protection('duplicates').max_open_threads, 0, 100, 'threads')}</div>${textSetting('asp-duplicate-rules', 'Rolling repeated-text limits', 'Normalized text ignores case, spacing, zero-width characters, and common lookalikes.', '4 msgs per 25 sec')}`),
    card('inhuman', `<div class="antispam-check-grid">${[
      ['non_abc', 'Symbol floods', 'Messages dominated by non-alphabetic characters.'],
      ['tall_messages', 'Tall messages', 'Excessively vertical text.'],
      ['empty_lines', 'Empty-line floods', 'Large blocks of blank lines.'],
      ['unknown_chars', 'Invisible/unknown Unicode', 'Suspicious formatting and control characters.'],
      ['ascii', 'Character walls', 'Large ASCII-art or repeated-character walls.'],
      ['copy_paste', 'Paste-risk signal', 'Large content posted without recent typing evidence.'],
    ].map(([key, label, copy]) => toggleSetting(`asp-integrity-${key}`, label, copy)).join('')}</div>`),
    card('anti_macro', `<div class="antispam-field-grid">${antiSpamNumber('asp-macro-samples', 'Sample size', 'Messages required before cadence can match.', protection('anti_macro').samples, 6, 30, 'msgs')}${antiSpamNumber('asp-macro-tolerance', 'Timing tolerance', 'Allowed interval variation.', protection('anti_macro').tolerance, 1, 50, '%')}${antiSpamRelation('Cadence interval range', 'Only message intervals inside this range are tested for automation.', { id: 'asp-macro-min', value: protection('anti_macro').min_interval, min: 1, max: 300, unit: 'sec' }, 'to', { id: 'asp-macro-max', value: protection('anti_macro').max_interval, min: 2, max: 3600, unit: 'sec' }, 'range')}</div>`),
    card('evasion', `<div class="antispam-field-grid">${antiSpamNumber('asp-evasion-zero', 'Invisible characters', 'Count needed to signal obfuscation.', protection('evasion').zero_width, 1, 100, 'chars')}${antiSpamNumber('asp-evasion-marks', 'Combining marks', 'Count needed to signal Zalgo-style mutation.', protection('evasion').combining_marks, 1, 100, 'marks')}${antiSpamNumber('asp-evasion-spoilers', 'Spoiler segments', 'Count needed to signal a spoiler wall.', protection('evasion').spoiler_segments, 1, 100, 'segments')}${antiSpamNumber('asp-evasion-resend', 'Delete-resend window', 'How long matching self-deleted content remains weak evidence. Set 0 to disable.', protection('evasion').resend_window, 0, 300, 'sec')}${antiSpamRelation('Rapid edit limit', 'Signals repeated editing when this count is reached inside the window.', { id: 'asp-evasion-edits', value: protection('evasion').max_edits, min: 2, max: 25, unit: 'edits' }, 'per', { id: 'asp-evasion-edit-window', value: protection('evasion').edit_window, min: 2, max: 300, unit: 'sec' }, 'limit')}</div>`),
    card('coordinated', `<div class="antispam-field-grid">${antiSpamRelation('Campaign participation limit', 'Requires this many distinct members to repeat a normalized payload inside the window.', { id: 'asp-coordinated-users', value: protection('coordinated').unique_users, min: 2, max: 25, unit: 'users' }, 'per', { id: 'asp-coordinated-window', value: protection('coordinated').window, min: 3, max: 300, unit: 'sec' }, 'campaign')}${antiSpamNumber('asp-coordinated-length', 'Minimum payload length', 'Short generic messages are ignored.', protection('coordinated').min_length, 6, 200, 'chars')}</div>`),
  ].join('');
}

function renderAntiSpamEditor(config, body) {
  const health = config.health || {};
  const coverage = health.coverage || {};
  const runtime = health.runtime || {};
  const warnings = health.warnings || [];
  const knownChannels = new Set(state.channels.map(channel => String(channel.id)));
  const knownRoles = new Set(state.roles.map(role => String(role.id)));
  const unresolvedChannels = (config.ignored_channels || []).filter(id => !knownChannels.has(String(id)));
  const unresolvedRoles = (config.trusted_roles || []).filter(id => !knownRoles.has(String(id)));
  body.innerHTML = `
    <section class="antispam-hero">
      <div><p>ANTI-SPAM CONTROL CENTER</p><h3>${config.enabled ? 'Protection enabled' : 'Protection disabled'}</h3><span>Seven independent protections · changes save together · current values are preloaded</span></div>
      <div class="antispam-metrics"><span><strong>${antiSpamModules.filter(([key]) => config.protections?.[key]?.enabled).length}/7</strong> modules</span><span><strong>${coverage.protected ?? 0}/${coverage.total ?? 0}</strong> channels</span><span><strong>${warnings.length}</strong> health warning${warnings.length === 1 ? '' : 's'}</span></div>
    </section>
    <details class="antispam-section" open><summary><span><b>1</b><span><strong>System & rollout</strong><small>Choose whether AntiSpam watches, removes, or fully enforces.</small></span></span></summary><div class="antispam-section-body">
      ${toggleSetting('mod-enabled', 'Enable AntiSpam', 'Process messages with the enabled protections below.')}
      <label class="dialog-setting dialog-setting-stack"><span><strong>Response mode</strong><small><b>Observe</b> records incidents only. <b>Delete only</b> removes evidence without member punishment. <b>Enforce</b> applies the response policy.</small></span><select id="asp-mode"><option value="observe">Observe · log without acting</option><option value="delete">Delete only · remove detected spam</option><option value="enforce">Enforce · delete and apply policies</option></select></label>
      <p class="dialog-warning">AntiSpam exempts administrators, moderators, trusted roles, trusted members, and ignored channel or category scopes.</p>
    </div></details>
    <details class="antispam-section" open><summary><span><b>2</b><span><strong>Protection modules</strong><small>Every detector is editable. Disabling one preserves its values.</small></span></span></summary><div class="antispam-section-body antispam-protection-list">${antiSpamProtectionCards(config)}</div></details>
    <details class="antispam-section"><summary><span><b>3</b><span><strong>Response policies</strong><small>Set a default response and precise overrides for each signal.</small></span></span></summary><div class="antispam-section-body"><div class="antispam-policy-help"><b>Adaptive timeout</b> escalates 5m → 10m → 20m → 40m, caps at 24h, and resets after 1h. Kick and Ban require explicit confirmation below.</div><div class="antispam-policy-list">${antiSpamPolicyRows(config)}</div><label class="antispam-danger-confirm" hidden><input id="asp-danger-confirm" type="checkbox"><span><strong>I confirm Kick/Ban policies</strong><small>These responses remove a member from the server when their detector triggers.</small></span></label></div></details>
    <details class="antispam-section"><summary><span><b>4</b><span><strong>Exclusions & trusted members</strong><small>Control exactly where and for whom protection is bypassed.</small></span></span></summary><div class="antispam-section-body"><div class="dialog-split">${selectSetting('mod-ignored', 'Ignored channels or categories', 'Selected scopes bypass all AntiSpam processing.', true)}${selectSetting('asp-trusted-roles', 'Trusted roles', 'Members with these roles bypass all AntiSpam processing.', true)}</div><div class="dialog-split">${textSetting('asp-unresolved-channels', 'Unresolved channel IDs', 'Preserved IDs not currently visible to the dashboard. One Discord ID per line.', '123456789012345678')}${textSetting('asp-unresolved-roles', 'Unresolved role IDs', 'Preserved role IDs not currently visible to the dashboard. One per line.', '123456789012345678')}</div>${textSetting('asp-trusted-members', 'Trusted member IDs', 'Specific members who always bypass AntiSpam. One Discord user ID per line.', '123456789012345678')}</div></details>
    <details class="antispam-section"><summary><span><b>5</b><span><strong>Health & live activity</strong><small>Permission readiness, coverage, and telemetry since the bot restarted.</small></span></span></summary><div class="antispam-section-body"><div class="antispam-health-grid"><span><strong>${runtime.processed ?? 0}</strong><small>messages processed</small></span><span><strong>${runtime.signals ?? 0}</strong><small>signals matched</small></span><span><strong>${runtime.incidents ?? 0}</strong><small>incidents</small></span><span><strong>${runtime.unique_users ?? 0}</strong><small>unique members</small></span><span><strong>${runtime.deleted ?? 0}</strong><small>messages removed</small></span><span><strong>${runtime.timeouts ?? 0}</strong><small>timeouts</small></span></div><div class="antispam-coverage"><strong>Channel coverage</strong><p>${coverage.protected ?? 0} protected · ${coverage.delete_ready ?? 0} delete-ready · ${coverage.ignored ?? 0} ignored · ${coverage.inaccessible ?? 0} not visible</p></div><div class="antispam-warning-list">${warnings.length ? warnings.map(warning => `<p><span>!</span>${escapeHtml(String(warning).replaceAll('**', ''))}</p>`).join('') : '<p class="ready"><span>✓</span>Configuration and bot permissions are ready.</p>'}</div>${runtime.top_module ? `<p class="antispam-top-rule">Most-triggered signal: <strong>${escapeHtml(String(runtime.top_module).replaceAll('_', ' '))}</strong></p>` : '<p class="antispam-top-rule">No AntiSpam activity recorded since restart.</p>'}</div></details>`;

  setEnabled('mod-enabled', config.enabled);
  byId('asp-mode').value = config.mode || 'enforce';
  for (const [key] of antiSpamModules) setEnabled(`asp-${key}-enabled`, config.protections?.[key]?.enabled);
  setEnabled('asp-pings-ghost', config.protections.mass_pings.ghost_pings);
  for (const key of ['non_abc', 'tall_messages', 'empty_lines', 'unknown_chars', 'ascii', 'copy_paste']) setEnabled(`asp-integrity-${key}`, config.protections.inhuman[key]);
  byId('asp-rate-rules').value = antiSpamRulesText(config.protections.rate_limit.thresholds);
  byId('asp-pings-rules').value = antiSpamRulesText(config.protections.mass_pings.thresholds);
  byId('asp-duplicate-rules').value = antiSpamRulesText(config.protections.duplicates.thresholds);
  setSelectOptions(byId('mod-ignored'), state.channels, config.ignored_channels, '# ');
  setSelectOptions(byId('asp-trusted-roles'), state.roles, config.trusted_roles, '@ ');
  byId('asp-unresolved-channels').value = unresolvedChannels.join('\n');
  byId('asp-unresolved-roles').value = unresolvedRoles.join('\n');
  byId('asp-trusted-members').value = (config.trusted_members || []).join('\n');
  const syncPolicyDanger = () => {
    const danger = [...body.querySelectorAll('[data-antispam-policy]')].some(select => ['kick', 'ban'].includes(select.value));
    body.querySelector('.antispam-danger-confirm').hidden = !danger;
  };
  for (const select of body.querySelectorAll('[data-antispam-policy]')) {
    select.addEventListener('change', () => {
      select.closest('.antispam-policy-row').querySelector('.antispam-custom-timeout').hidden = select.value !== 'custom';
      syncPolicyDanger();
    });
  }
  for (const [key] of antiSpamModules) byId(`asp-${key}-enabled`).addEventListener('change', event => {
    body.querySelector(`[data-module-state="${key}"]`).textContent = event.target.checked ? 'Enabled' : 'Disabled';
  });
  syncPolicyDanger();
}

function renderAntiRaidEditor(config, body) {
  const protections = config.protections || {};
  const burst = protections.join_burst || {};
  const risk = protections.suspicious_accounts || {};
  const staff = protections.destructive_actions || {};
  const response = config.response || {};
  const health = config.health || {};
  const runtime = health.runtime || {};
  const warnings = health.warnings || [];
  const knownRoles = new Set(state.roles.map(role => String(role.id)));
  const unresolvedRoles = (config.trusted_roles || []).filter(id => !knownRoles.has(String(id)));
  const enabledCount = Object.values(protections).filter(protection => protection.enabled).length;
  const watchedCount = ['watch_bans', 'watch_kicks', 'watch_channels', 'watch_roles', 'watch_webhooks'].filter(key => staff[key]).length;
  const lockStatus = runtime.active_lockdown
    ? `Active · ${runtime.temporary_bans || 0} temporary bans await release`
    : 'Open · no active lockdown';
  body.innerHTML = `
    <section class="antiraid-hero">
      <div><p>ANTI-RAID CONTROL CENTER</p><h3>${runtime.active_lockdown ? 'Lockdown active' : config.enabled ? 'Defense online' : 'Defense offline'}</h3><span>Layered detection · persisted containment · explainable incident telemetry</span></div>
      <div class="antiraid-metrics"><span><strong>${enabledCount}/3</strong> defenses</span><span><strong>${runtime.incidents ?? 0}</strong> incidents</span><span><strong>${runtime.contained_members ?? 0}</strong> contained</span></div>
    </section>
    <details class="antiraid-section" open><summary><span><b>1</b><span><strong>System & rollout</strong><small>Observe safely first, then enforce the selected responses.</small></span></span></summary><div class="antiraid-section-body">
      ${toggleSetting('mod-enabled', 'Enable Raid Protection', 'Correlate member joins and destructive Discord audit activity.')}
      <label class="dialog-setting dialog-setting-stack"><span><strong>Response mode</strong><small><b>Observe</b> records evidence only. <b>Enforce</b> applies lockdown and compromised-account containment.</small></span><select id="raid-mode"><option value="observe">Observe · record without acting</option><option value="enforce">Enforce · contain detected raids</option></select></label>
      <div class="antiraid-lock-state ${runtime.active_lockdown ? 'active' : ''}"><span>${runtime.active_lockdown ? 'LOCKED' : 'OPEN'}</span><div><strong>${lockStatus}</strong><small>Lock state and temporary bans survive cog reloads and restart cleanup.</small></div></div>
    </div></details>
    <details class="antiraid-section" open><summary><span><b>2</b><span><strong>Join defense</strong><small>Use a broad member-flood limit plus a faster high-risk account cluster.</small></span></span></summary><div class="antiraid-section-body">
      <div class="antiraid-defense-card">${toggleSetting('raid-burst-enabled', 'Join burst shield', 'Trigger when total joins reach this rolling limit.')}<div class="antispam-field-grid">${antiSpamNumber('raid-burst-threshold', 'Join count', 'Members needed to trigger the broad burst shield.', burst.threshold ?? 10, 3, 100, 'joins')}${antiSpamNumber('raid-burst-window', 'Burst window', 'Rolling time window for total joins.', burst.window ?? 20, 3, 300, 'sec')}</div></div>
      <div class="antiraid-defense-card">${toggleSetting('raid-risk-enabled', 'New-account cluster', 'Trigger sooner when several young accounts arrive together.')}<div class="antispam-field-grid">${antiSpamNumber('raid-risk-threshold', 'Risk account count', 'High-risk accounts needed inside the cluster window.', risk.threshold ?? 4, 2, 50, 'accounts')}${antiSpamNumber('raid-risk-window', 'Risk window', 'Rolling window for high-risk accounts.', risk.window ?? 30, 3, 600, 'sec')}${antiSpamNumber('raid-risk-age', 'Maximum account age', 'Accounts this age or younger contribute to the cluster.', risk.max_account_age_hours ?? 24, 1, 8760, 'hours')}${toggleSetting('raid-risk-avatar', 'Require a blank avatar', 'Only young accounts without a custom avatar contribute to the risk cluster.')}</div></div>
    </div></details>
    <details class="antiraid-section" open><summary><span><b>3</b><span><strong>Compromised staff guard</strong><small>Correlate dangerous actions by one audit-log actor.</small></span></span></summary><div class="antiraid-section-body">
      ${toggleSetting('raid-staff-enabled', 'Enable staff guard', 'Keep administrators detectable unless they are explicitly trusted.')}
      <div class="antispam-field-grid">${antiSpamNumber('raid-staff-threshold', 'Action count', 'Destructive actions by one actor needed to trigger.', staff.threshold ?? 3, 2, 25, 'actions')}${antiSpamNumber('raid-staff-window', 'Action window', 'Rolling correlation window for each actor.', staff.window ?? 15, 3, 300, 'sec')}</div>
      <div class="antiraid-watch-grid">${[
        ['watch_bans', 'Bans', 'Mass member bans.'], ['watch_kicks', 'Kicks', 'Mass member kicks.'],
        ['watch_channels', 'Channel deletion', 'Deleted text, voice, forum, and category channels.'],
        ['watch_roles', 'Role deletion', 'Deleted permission and identity roles.'],
        ['watch_webhooks', 'Webhook deletion', 'Deleted outbound integrations.'],
      ].map(([key, label, copy]) => toggleSetting(`raid-${key}`, label, copy)).join('')}</div>
      <p class="dialog-warning">The server owner and Fate are always protected. Discord role hierarchy can prevent containment; readiness warnings below show missing permissions.</p>
    </div></details>
    <details class="antiraid-section"><summary><span><b>4</b><span><strong>Containment & delivery</strong><small>Choose explicit join/staff responses, lockdown duration, and incident routing.</small></span></span></summary><div class="antiraid-section-body">
      <div class="dialog-split"><label class="dialog-setting dialog-setting-stack"><span><strong>Join response</strong><small>Temporary Ban blocks re-entry and auto-unbans when the lockdown ends.</small></span><select id="raid-join-action"><option value="temporary_ban">Temporary ban · auto-release</option><option value="kick">Kick · immediate rejoin is possible</option></select></label><label class="dialog-setting dialog-setting-stack"><span><strong>Compromised staff response</strong><small>Strip Roles removes only manageable roles with dangerous permissions.</small></span><select id="raid-staff-action"><option value="strip_roles">Strip dangerous roles</option><option value="kick">Kick suspected actor</option><option value="ban">Ban suspected actor</option></select></label></div>
      <div class="dialog-split">${antiSpamNumber('raid-lock-minutes', 'Lockdown duration', 'Temporary bans are released automatically after this period.', response.lock_minutes ?? 10, 1, 1440, 'minutes')}${selectSetting('raid-alert-channel', 'Incident alert channel', 'Leave disabled to use Fate’s safe automatic channel fallback.')}</div>
      <label class="antispam-danger-confirm antiraid-danger-confirm" hidden><input id="raid-danger-confirm" type="checkbox"><span><strong>I confirm the Kick/Ban staff response</strong><small>This policy removes a suspected compromised staff account when its threshold is reached.</small></span></label>
    </div></details>
    <details class="antiraid-section"><summary><span><b>5</b><span><strong>Trusted bypasses</strong><small>Explicit exceptions for automation or people who must never trigger containment.</small></span></span></summary><div class="antiraid-section-body">
      ${selectSetting('raid-trusted-roles', 'Trusted roles', 'Every member with a selected role fully bypasses AntiRaid.', true)}
      <div class="dialog-split">${textSetting('raid-unresolved-roles', 'Unresolved trusted role IDs', 'Preserved IDs for roles not currently visible to the dashboard.', '123456789012345678')}${textSetting('raid-trusted-members', 'Trusted member IDs', 'One Discord user ID per line. The owner and Fate are always trusted.', '123456789012345678')}</div>
    </div></details>
    <details class="antiraid-section"><summary><span><b>6</b><span><strong>Health & live activity</strong><small>Permission readiness and telemetry since the bot restarted.</small></span></span></summary><div class="antiraid-section-body">
      <div class="antispam-health-grid antiraid-health-grid"><span><strong>${runtime.joins ?? 0}</strong><small>joins seen</small></span><span><strong>${runtime.suspicious_joins ?? 0}</strong><small>high-risk joins</small></span><span><strong>${runtime.destructive_actions ?? 0}</strong><small>dangerous actions</small></span><span><strong>${runtime.incidents ?? 0}</strong><small>incidents</small></span><span><strong>${runtime.contained_members ?? 0}</strong><small>members contained</small></span><span><strong>${runtime.failures ?? 0}</strong><small>failed actions</small></span></div>
      <div class="antispam-warning-list">${warnings.length ? warnings.map(warning => `<p><span>!</span>${escapeHtml(String(warning).replaceAll('**', ''))}</p>`).join('') : '<p class="ready"><span>✓</span>Configuration and bot permissions are ready.</p>'}</div>
      <p class="antispam-top-rule">Watching ${watchedCount}/5 destructive action types${runtime.top_trigger ? ` · most recent top trigger: <strong>${escapeHtml(String(runtime.top_trigger).replaceAll('_', ' '))}</strong>` : ' · no trigger has fired this session'}.</p>
    </div></details>`;

  setEnabled('mod-enabled', config.enabled);
  byId('raid-mode').value = config.mode || 'enforce';
  setEnabled('raid-burst-enabled', burst.enabled);
  setEnabled('raid-risk-enabled', risk.enabled);
  setEnabled('raid-risk-avatar', risk.require_no_avatar);
  setEnabled('raid-staff-enabled', staff.enabled);
  for (const key of ['watch_bans', 'watch_kicks', 'watch_channels', 'watch_roles', 'watch_webhooks']) setEnabled(`raid-${key}`, staff[key]);
  byId('raid-join-action').value = response.join_action || 'temporary_ban';
  byId('raid-staff-action').value = response.staff_action || 'strip_roles';
  channelOptions(byId('raid-alert-channel'), config.alert_channel_id, { disabled: true, emptyLabel: 'Automatic fallback' });
  setSelectOptions(byId('raid-trusted-roles'), state.roles, config.trusted_roles, '@ ');
  byId('raid-unresolved-roles').value = unresolvedRoles.join('\n');
  byId('raid-trusted-members').value = (config.trusted_members || []).join('\n');
  const syncDanger = () => {
    const dangerous = ['kick', 'ban'].includes(byId('raid-staff-action').value)
      && byId('raid-mode').value === 'enforce'
      && byId('raid-staff-enabled').checked;
    body.querySelector('.antiraid-danger-confirm').hidden = !dangerous;
  };
  byId('raid-staff-action').addEventListener('change', syncDanger);
  byId('raid-mode').addEventListener('change', syncDanger);
  byId('raid-staff-enabled').addEventListener('change', syncDanger);
  syncDanger();
}

function openModule(name) {
  const config = state.modules[name];
  const meta = moduleMeta[name];
  if (!config?.available || config.locked) return;
  state.editingModule = name;
  byId('module-dialog-title').textContent = meta.title;
  byId('module-dialog-copy').textContent = meta.copy;
  byId('module-dialog-icon').textContent = meta.icon;
  byId('module-dialog-icon').className = `module-dialog-icon ${meta.tone}`;
  byId('module-dialog').dataset.tone = meta.tone;
  byId('module-dialog').dataset.module = name;
  byId('module-delete').hidden = true;
  byId('module-save').hidden = false;
  byId('module-save').disabled = false;
  const body = byId('module-dialog-body');

  if (name === 'verification') {
    body.innerHTML = `
      ${toggleSetting('mod-enabled', 'Enable Member Verification', 'Ask new members to complete a private captcha before receiving access.')}
      <div class="dialog-split">
        ${selectSetting('mod-channel', 'Verification channel', 'Fate posts the shared verification message here.')}
        ${selectSetting('mod-verified-role', 'Verified role', 'Members receive this role after solving the captcha.')}
      </div>
      <div class="dialog-split">
        ${selectSetting('mod-temp-role', 'Restricted role (optional)', 'Remove this role after a member passes verification.')}
        ${selectSetting('mod-log-channel', 'Activity log (optional)', 'Post successful, failed, and expired attempts here.')}
      </div>
      <label class="dialog-setting dialog-setting-stack"><span><strong>Challenge time</strong><small>How long each member has to complete their private captcha.</small></span><div class="number-field"><input id="mod-time-limit" type="number" min="30" max="600" required><span>seconds</span></div></label>
      <div class="dialog-toggle-grid">
        ${toggleSetting('mod-auto-start', 'Start on join', 'Open the challenge automatically for new members.')}
        ${toggleSetting('mod-delete-after', 'Clean up completed challenges', 'Delete temporary challenge messages after success.')}
        ${toggleSetting('mod-kick-on-fail', 'Kick on timeout', 'Remove members who do not finish in time.')}
      </div>
      <p class="dialog-warning">${config.panel_active ? 'The shared verification message is active. Saving updates it.' : 'Enabling verification posts its shared message after you save.'}</p>`;
    setEnabled('mod-enabled', config.enabled);
    populateChannelSelect('mod-channel', config.channel_id);
    populateRoleSelect('mod-verified-role', config.verified_role_id);
    populateRoleSelect('mod-temp-role', config.temp_role_id);
    populateChannelSelect('mod-log-channel', config.log_channel);
    byId('mod-time-limit').value = String(config.time_limit);
    setEnabled('mod-auto-start', config.auto_start);
    setEnabled('mod-delete-after', config.delete_after);
    setEnabled('mod-kick-on-fail', config.kick_on_fail);
  } else if (name === 'autorole') {
    body.innerHTML = `
      ${toggleSetting('mod-enabled', 'Enable Auto Roles', 'Give the selected roles to new members automatically.')}
      ${toggleSetting('mod-wait-verify', 'Wait for verification', 'Give roles only after the member passes verification.')}
      <div class="dialog-setting dialog-setting-stack"><span><strong>Roles and timing</strong><small>Use 0 seconds to assign a role immediately. Delayed roles must wait at least 60 seconds.</small></span><div class="autorole-rows" id="autorole-rows"></div><button class="inline-add" id="autorole-add" type="button">+ Add role</button></div>`;
    setEnabled('mod-enabled', config.enabled);
    setEnabled('mod-wait-verify', config.wait_for_verify);
    config.roles.forEach(item => addAutoRoleRow(item.role_id, item.delay));
    if (!config.roles.length) addAutoRoleRow();
    byId('autorole-add').addEventListener('click', () => addAutoRoleRow());
  } else if (name === 'chatfilter') {
    body.innerHTML = `
      ${toggleSetting('mod-enabled', 'Enable Chat Filter', 'Remove messages that match the rules below.')}
      <div class="dialog-split">${textSetting('mod-blacklist', 'Blocked words and phrases', 'Add one word or phrase per line.', 'blocked phrase')} ${textSetting('mod-whitelist', 'Allowed exceptions', 'Add one exception per line so safe messages are not removed.', 'allowed phrase')}</div>
      ${selectSetting('mod-ignored', 'Ignored channels', 'Selected channels bypass the filter.', true)}
      <div class="dialog-toggle-grid">${toggleSetting('mod-regex', 'Advanced pattern matching', 'Treat blocked entries as patterns instead of exact text. Use this only if you know pattern syntax.')} ${toggleSetting('mod-nicks', 'Check nicknames', 'Apply the same rules when a nickname changes.')} ${toggleSetting('mod-bots', 'Check bot messages', 'Apply the filter to messages from other bots.')} ${toggleSetting('mod-webhooks', 'Check webhook messages', 'Apply the filter to webhook messages.')} ${toggleSetting('mod-phishing', 'Block phishing links', 'Remove links that Fate identifies as phishing.')}</div>`;
    setEnabled('mod-enabled', config.enabled);
    byId('mod-blacklist').value = config.blacklist.join('\n');
    byId('mod-whitelist').value = config.whitelist.join('\n');
    setSelectOptions(byId('mod-ignored'), state.channels, config.ignored_channels, '# ');
    setEnabled('mod-regex', config.regex);
    setEnabled('mod-nicks', config.filter_nicks);
    setEnabled('mod-bots', config.filter_bots);
    setEnabled('mod-webhooks', config.filter_webhooks);
    setEnabled('mod-phishing', config.filter_phishing);
  } else if (name === 'logger') {
    state.loggerEditorSource = 'json';
    const defaultLocalArchive = {
      enabled: false,
      retention_days: 30,
      cache_messages: true,
      store_attachments: false,
      attachment_size_limit_mb: 25,
    };
    const localArchive = config.configuration?.local_archive || config.local_archive || defaultLocalArchive;
    const loggingConfiguration = config.configuration || {
      channel: config.channel_id,
      channels: {},
      secure: config.secure,
      ignored_roles: [],
      ignored_channels: config.ignored_channels,
      ignored_bots: [],
      disabled: [],
      theme: config.theme,
      local_archive: { ...localArchive },
    };
    if (!loggingConfiguration.local_archive) loggingConfiguration.local_archive = { ...localArchive };
    body.innerHTML = `
      ${toggleSetting('mod-enabled', 'Enable Logging', 'Record the selected server activity for your staff team.')}
      ${selectSetting('mod-channel', 'Primary logging channel', 'Choose where Fate should post server records by default.')}
      <fieldset class="dialog-setting dialog-setting-stack logger-appearance"><legend>Message colors</legend><small>Choose what the color beside each Discord log should communicate.</small><div class="logger-appearance-options">
        <label><input name="mod-theme" type="radio" value=""><span class="logger-appearance-preview event"><i></i><i></i><i></i></span><span><strong>Colors by activity</strong><small>Recommended. Joins, moderation, messages, and channel changes keep distinct colors that are easy to scan.</small></span></label>
        <label><input name="mod-theme" type="radio" value="Role Color"><span class="logger-appearance-preview member"><i></i><i></i><i></i></span><span><strong>Colors by member role</strong><small>Logs about a member use their highest Discord role color. Other activity keeps its usual color.</small></span></label>
        <label><input name="mod-theme" type="radio" value="RGB"><span class="logger-appearance-preview cycle"><i></i><i></i><i></i></span><span><strong>Rotating color cycle</strong><small>Colors flow through a decorative spectrum and no longer identify the kind of activity.</small></span></label>
      </div></fieldset>
      ${toggleSetting('mod-secure', 'Lock settings to server owner', 'Only the server owner can change these Logging settings.')}
      <div class="dialog-setting dialog-setting-stack"><span><strong>Activity to record</strong><small>Select the types of server changes staff should see in Logging.</small></span><div class="check-chip-grid" id="logger-groups"></div></div>
      ${selectSetting('mod-ignored', 'Ignored channels', 'Do not create Logging records for activity from these channels.', true)}
      <section class="logger-archive-panel" aria-labelledby="logger-archive-title">
        <div class="logger-archive-heading"><span aria-hidden="true">◎</span><div><strong id="logger-archive-title">Saved Logging history</strong><small>Keep a private, searchable copy on the computer running Fate, including records Discord could not deliver. Fate keeps up to 1 GB of retained history per server and removes the oldest saved records as it fills.</small></div><b>Up to 1 GB retained per server</b></div>
        ${toggleSetting('logger-archive-enabled', 'Store history locally', 'This is off by default. Turn it on to search farther back than Discord’s currently loaded messages.')}
        <div class="logger-archive-fields" id="logger-archive-fields">
          <label class="logger-archive-retention"><span><strong>Keep history for</strong><small>Older saved records are removed automatically.</small></span><span><input id="logger-archive-retention" type="number" min="1" max="365" step="1"><em>days</em></span></label>
          ${toggleSetting('logger-archive-cache', 'Recover uncached message changes', 'Save message snapshots so edits and deletions can still include useful context after Discord removes them from memory.')}
          ${toggleSetting('logger-archive-attachments', 'Store images and files', 'Download attachments that fit your per-file limit. Stored files count toward the 1 GB of retained history for this server.')}
          <label class="logger-archive-retention logger-archive-file-limit"><span><strong>Maximum size per file</strong><small>Files over this limit are not downloaded. Fate still keeps the embed details, filename, size, and Discord link.</small></span><span><input id="logger-archive-file-limit" type="number" min="1" max="25" step="1"><em>MB</em></span></label>
        </div>
        <div class="logger-archive-foot"><span id="logger-archive-usage">${localArchive.enabled ? 'Reading current storage use…' : 'Off by default · no server history is stored'}</span><small>Files stay as embed and link details unless local file storage is turned on.</small></div>
      </section>
      <section class="logging-json-editor" aria-labelledby="logging-json-title">
        <div><strong id="logging-json-title">Advanced logging JSON</strong><small>The complete current configuration is loaded below, including event routing, ignored bots and roles, and custom colors.</small></div>
        <textarea id="logger-json" rows="12" spellcheck="false" aria-label="Advanced logging JSON"></textarea>
        <p class="logging-json-status" id="logger-json-status"></p>
      </section>
      <section class="activity-log-browser" aria-labelledby="activity-log-browser-title">
        <div class="activity-log-browser-heading"><div><span class="activity-log-live-dot"></span><div><strong id="activity-log-browser-title">${localArchive.enabled ? 'Saved logging history' : 'Latest logging activity'}</strong><small>${localArchive.enabled ? 'Search all locally saved records, including activity that Discord could not deliver.' : 'Search the records Fate has already delivered to your logging channels.'}</small></div></div><button class="activity-log-refresh" id="activity-log-refresh" type="button">↻ Refresh</button></div>
        <div class="activity-log-tools">
          <label class="activity-log-search"><span>⌕</span><input id="activity-log-search" type="search" placeholder="Search people, channels, actions, or details…" autocomplete="off" aria-label="Search logging records"></label>
          <label><span>Activity type</span><select id="activity-log-type"><option value="">All activity types</option></select></label>
          <label><span>When</span><select id="activity-log-time"><option value="">All loaded</option><option value="3600000">Last hour</option><option value="86400000">Last 24 hours</option><option value="604800000">Last 7 days</option><option value="2592000000">Last 30 days</option></select></label>
        </div>
        <div class="activity-log-summary"><span id="activity-log-count">Loading latest activity…</span><span class="activity-log-warning" id="activity-log-warning" hidden></span></div>
        <div class="activity-log-results" id="activity-log-results" aria-live="polite"></div>
        <button class="activity-log-more" id="activity-log-more" type="button" hidden>Load older activity</button>
      </section>`;
    setEnabled('mod-enabled', config.enabled);
    populateChannelSelect('mod-channel', config.channel_id);
    const selectedTheme = config.theme || '';
    document.querySelectorAll('input[name="mod-theme"]').forEach(input => {
      input.checked = input.value === selectedTheme;
    });
    setEnabled('mod-secure', config.secure);
    setEnabled('logger-archive-enabled', localArchive.enabled);
    byId('logger-archive-retention').value = String(localArchive.retention_days ?? 30);
    setEnabled('logger-archive-cache', localArchive.cache_messages !== false);
    setEnabled('logger-archive-attachments', localArchive.store_attachments === true);
    byId('logger-archive-file-limit').value = String(localArchive.attachment_size_limit_mb ?? 25);
    renderCheckboxGrid('logger-groups', loggerGroups, config.groups);
    setSelectOptions(byId('mod-ignored'), state.channels, config.ignored_channels, '# ');
    byId('logger-json').value = JSON.stringify(loggingConfiguration, null, 2);
    byId('logger-json').addEventListener('input', () => {
      state.loggerEditorSource = 'json';
      updateLoggerJsonStatus();
    });
    for (const control of body.querySelectorAll('#mod-enabled, #mod-channel, input[name="mod-theme"], #mod-secure, #logger-groups input, #mod-ignored, #logger-archive-enabled, #logger-archive-retention, #logger-archive-cache, #logger-archive-attachments, #logger-archive-file-limit')) {
      control.addEventListener('change', useLoggerControls);
    }
    byId('logger-archive-enabled').addEventListener('change', syncLoggerArchiveUi);
    byId('logger-archive-attachments').addEventListener('change', syncLoggerArchiveUi);
    syncLoggerArchiveUi();
    updateLoggerJsonStatus();
    setupActivityLogBrowser();
  } else if (name === 'modmail') {
    body.innerHTML = `
      ${toggleSetting('mod-enabled', 'Enable Modmail', 'Let members privately contact staff about their moderation cases.')}
      ${selectSetting('mod-channel', 'Staff channel', 'Fate creates one private case thread here for each conversation.')}
      <p class="dialog-warning">Fate needs permission to view the audit log and create, send, and manage threads in this channel.</p>
      <section class="modmail-entry-browser" aria-labelledby="modmail-entry-title">
        <div class="modmail-entry-heading">
          <div><span class="modmail-entry-live-dot"></span><div><strong id="modmail-entry-title">Discord conversations</strong><small>Preview the latest message from active, archived, and closed Modmail cases.</small></div></div>
          <button class="modmail-entry-refresh" id="modmail-entry-refresh" type="button">↻ Refresh</button>
        </div>
        <p class="modmail-entry-summary" id="modmail-entry-summary">Loading conversations…</p>
        <div class="modmail-entry-list" id="modmail-entry-list" aria-live="polite"></div>
      </section>`;
    setEnabled('mod-enabled', config.enabled);
    populateChannelSelect('mod-channel', config.channel_id);
    byId('modmail-entry-refresh').addEventListener('click', loadModmailEntries);
  } else if (name === 'anti_spam') {
    renderAntiSpamEditor(config, body);
  } else if (name === 'anti_raid') {
    renderAntiRaidEditor(config, body);
  } else if (name === 'welcome' || name === 'leave') {
    renderMessageModule(config, name);
  } else if (name === 'restore_roles') {
    body.innerHTML = `${toggleSetting('mod-enabled', 'Enable Restore Roles', 'Remember a member’s roles and return them when they rejoin.')}${toggleSetting('mod-perms', 'Include staff roles', 'For safety, only the server owner can change this option.') }<p class="dialog-warning">Fate can only restore roles below its highest role.</p>`;
    setEnabled('mod-enabled', config.enabled);
    setEnabled('mod-perms', config.allow_permissions);
  } else if (name === 'selfroles') {
    renderSelfRoleEditor();
  } else if (name === 'giveaways') {
    byId('module-save').hidden = true;
    const records = config.giveaways || [];
    const active = Number(config.active_count || 0);
    const recent = Number(config.recent_count || 0);
    const entries = Number(config.total_entries || 0);
    const cards = records.map(item => {
      const endDate = item.end_at ? new Date(item.end_at) : null;
      const timing = endDate && !Number.isNaN(endDate.getTime())
        ? `${item.status === 'active' ? 'Ends' : 'Ended'} ${formatDiscordRelativeTime(endDate)}`
        : item.status === 'active' ? 'Active now' : 'Recently ended';
      const link = item.url
        ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open in Discord ↗</a>`
        : '<span>Discord message unavailable</span>';
      return `<article class="giveaway-dashboard-entry ${item.status === 'active' ? 'active' : 'ended'}">
        <header><div><i></i><strong>${escapeHtml(item.prize)}</strong></div><span>${item.status === 'active' ? 'ACTIVE' : 'ENDED'}</span></header>
        <p>${Number(item.entry_count || 0)} entr${Number(item.entry_count || 0) === 1 ? 'y' : 'ies'} · ${Number(item.winner_count || 1)} winner${Number(item.winner_count || 1) === 1 ? '' : 's'} · ${escapeHtml(timing)}</p>
        <footer><small>Giveaway ID ${escapeHtml(item.id)}</small>${link}</footer>
      </article>`;
    }).join('');
    body.innerHTML = `
      <section class="giveaway-dashboard-hero">
        <div><p>LIVE GIVEAWAY OPERATIONS</p><h3>${active ? `${active} active giveaway${active === 1 ? '' : 's'}` : 'Ready for the next giveaway'}</h3><span>Create and manage giveaways in Discord with <strong>/giveaway</strong>. This dashboard mirrors their live state.</span></div>
        <div class="giveaway-dashboard-metrics"><span><strong>${active}</strong><small>active</small></span><span><strong>${entries}</strong><small>entries</small></span><span><strong>${recent}</strong><small>recently ended</small></span></div>
      </section>
      <section class="giveaway-dashboard-list" aria-label="Current and recent giveaways">
        ${cards || '<div class="giveaway-dashboard-empty"><span>◇</span><div><strong>No giveaways yet</strong><small>Use /giveaway create or /giveaway advanced in Discord. Active giveaways will appear here automatically.</small></div></div>'}
      </section>
      <p class="dialog-warning">Winner selection, rerolls, deletion, and eligibility changes stay in Discord so Fate can apply channel and role permission checks at the moment each action runs.</p>`;
  } else if (name === 'starboard') {
    renderStarboardEditor(config, body);
  } else if (name === 'vc_log') {
    body.innerHTML = `${toggleSetting('mod-enabled', 'Enable Voice Activity Log', 'Record voice channel joins, leaves, and moves.')}${selectSetting('mod-channel', 'Activity channel', 'Choose where Fate should post voice activity.')}${toggleSetting('mod-clean', 'Keep the channel clean', 'Remove unrelated messages after a short delay.')}`;
    setEnabled('mod-enabled', config.enabled);
    populateChannelSelect('mod-channel', config.channel_id);
    setEnabled('mod-clean', config.keep_clean);
  }
  openDialog('module-dialog');
  if (name === 'logger') loadActivityLogs({ reset: true });
  if (name === 'modmail') loadModmailEntries();
}

function collectModuleSettings(name) {
  if (name === 'verification') {
    const enabled = byId('mod-enabled').checked;
    const channelId = selectedChannel(byId('mod-channel').value) || null;
    const verifiedRoleId = selectedChannel(byId('mod-verified-role').value) || null;
    if (enabled && !channelId) throw new Error('Choose a verification channel before enabling verification.');
    if (enabled && !verifiedRoleId) throw new Error('Choose the role verified members should receive.');
    return {
      enabled,
      channel_id: channelId,
      verified_role_id: verifiedRoleId,
      temp_role_id: selectedChannel(byId('mod-temp-role').value) || null,
      log_channel: selectedChannel(byId('mod-log-channel').value) || null,
      time_limit: Number(byId('mod-time-limit').value),
      auto_start: byId('mod-auto-start').checked,
      delete_after: byId('mod-delete-after').checked,
      kick_on_fail: byId('mod-kick-on-fail').checked,
    };
  }
  if (name === 'autorole') {
    return {
      enabled: byId('mod-enabled').checked,
      wait_for_verify: byId('mod-wait-verify').checked,
      roles: [...document.querySelectorAll('.autorole-row')].map(row => ({
        role_id: row.querySelector('.autorole-role').value,
        delay: Number(row.querySelector('.autorole-delay').value),
      })).filter(item => item.role_id !== '__off'),
    };
  }
  if (name === 'chatfilter') {
    return {
      enabled: byId('mod-enabled').checked,
      blacklist: splitLines(byId('mod-blacklist').value),
      whitelist: splitLines(byId('mod-whitelist').value),
      ignored_channels: selectedValues(byId('mod-ignored')),
      regex: byId('mod-regex').checked,
      filter_nicks: byId('mod-nicks').checked,
      filter_bots: byId('mod-bots').checked,
      filter_webhooks: byId('mod-webhooks').checked,
      filter_phishing: byId('mod-phishing').checked,
    };
  }
  if (name === 'logger') {
    let configuration = null;
    if (state.loggerEditorSource === 'json') {
      try {
        configuration = JSON.parse(byId('logger-json').value);
      } catch (error) {
        throw new Error(`The logging JSON is not valid: ${error.message}`);
      }
    }
    return {
      enabled: byId('mod-enabled').checked,
      channel_id: selectedChannel(byId('mod-channel').value) || null,
      secure: byId('mod-secure').checked,
      theme: document.querySelector('input[name="mod-theme"]:checked')?.value
        ?? state.modules.logger.configuration?.theme
        ?? null,
      groups: [...document.querySelectorAll('#logger-groups input:checked')].map(input => input.value),
      ignored_channels: selectedValues(byId('mod-ignored')),
      local_archive: {
        enabled: byId('logger-archive-enabled').checked,
        retention_days: Number(byId('logger-archive-retention').value),
        cache_messages: byId('logger-archive-cache').checked,
        store_attachments: byId('logger-archive-attachments').checked,
        attachment_size_limit_mb: Number(byId('logger-archive-file-limit').value),
      },
      configuration,
    };
  }
  if (name === 'modmail') {
    return {
      enabled: byId('mod-enabled').checked,
      channel_id: selectedChannel(byId('mod-channel').value) || null,
    };
  }
  if (name === 'anti_spam') {
    const number = id => Number(byId(id).value);
    const punishments = {};
    let hasDangerousPolicy = false;
    for (const select of document.querySelectorAll('[data-antispam-policy]')) {
      let action = select.value;
      if (action === 'custom') {
        const seconds = Number(select.closest('.antispam-policy-row').querySelector('.antispam-custom-timeout input').value);
        if (!Number.isInteger(seconds) || seconds < 10 || seconds > 2419200) throw new Error('Custom timeouts must be between 10 seconds and 28 days.');
        action = `timeout:${seconds}`;
      }
      if (['kick', 'ban'].includes(action)) hasDangerousPolicy = true;
      punishments[select.dataset.antispamPolicy] = action;
    }
    if (hasDangerousPolicy && !byId('asp-danger-confirm').checked) throw new Error('Confirm the Kick/Ban policies before saving.');
    const protections = {
      rate_limit: { enabled: byId('asp-rate_limit-enabled').checked, thresholds: parseAntiSpamRules(byId('asp-rate-rules').value, 'Flood control') },
      mass_pings: { enabled: byId('asp-mass_pings-enabled').checked, per_message: number('asp-pings-per-message'), ghost_pings: byId('asp-pings-ghost').checked, thresholds: parseAntiSpamRules(byId('asp-pings-rules').value, 'Mention bursts') },
      duplicates: { enabled: byId('asp-duplicates-enabled').checked, per_message: number('asp-duplicate-segments'), same_link: number('asp-link-window'), same_image: number('asp-image-window'), sticker: number('asp-sticker-window'), same_sticker: number('asp-same-sticker-window'), max_open_threads: number('asp-thread-limit'), thresholds: parseAntiSpamRules(byId('asp-duplicate-rules').value, 'Repeated text') },
      inhuman: { enabled: byId('asp-inhuman-enabled').checked, ...Object.fromEntries(['non_abc', 'tall_messages', 'empty_lines', 'unknown_chars', 'ascii', 'copy_paste'].map(key => [key, byId(`asp-integrity-${key}`).checked])) },
      anti_macro: { enabled: byId('asp-anti_macro-enabled').checked, samples: number('asp-macro-samples'), tolerance: number('asp-macro-tolerance'), min_interval: number('asp-macro-min'), max_interval: number('asp-macro-max') },
      evasion: { enabled: byId('asp-evasion-enabled').checked, zero_width: number('asp-evasion-zero'), combining_marks: number('asp-evasion-marks'), spoiler_segments: number('asp-evasion-spoilers'), max_edits: number('asp-evasion-edits'), edit_window: number('asp-evasion-edit-window'), resend_window: number('asp-evasion-resend') },
      coordinated: { enabled: byId('asp-coordinated-enabled').checked, window: number('asp-coordinated-window'), unique_users: number('asp-coordinated-users'), min_length: number('asp-coordinated-length') },
    };
    if (byId('mod-enabled').checked && !Object.values(protections).some(protection => protection.enabled)) throw new Error('Enable at least one AntiSpam protection.');
    return {
      enabled: byId('mod-enabled').checked,
      schema_version: 2,
      mode: byId('asp-mode').value,
      protections,
      punishments,
      ignored_channels: [...new Set([...selectedValues(byId('mod-ignored')), ...splitLines(byId('asp-unresolved-channels').value)])],
      trusted_roles: [...new Set([...selectedValues(byId('asp-trusted-roles')), ...splitLines(byId('asp-unresolved-roles').value)])],
      trusted_members: splitLines(byId('asp-trusted-members').value),
    };
  }
  if (name === 'anti_raid') {
    const number = id => Number(byId(id).value);
    const protections = {
      join_burst: {
        enabled: byId('raid-burst-enabled').checked,
        threshold: number('raid-burst-threshold'),
        window: number('raid-burst-window'),
      },
      suspicious_accounts: {
        enabled: byId('raid-risk-enabled').checked,
        threshold: number('raid-risk-threshold'),
        window: number('raid-risk-window'),
        max_account_age_hours: number('raid-risk-age'),
        require_no_avatar: byId('raid-risk-avatar').checked,
      },
      destructive_actions: {
        enabled: byId('raid-staff-enabled').checked,
        threshold: number('raid-staff-threshold'),
        window: number('raid-staff-window'),
        ...Object.fromEntries(['watch_bans', 'watch_kicks', 'watch_channels', 'watch_roles', 'watch_webhooks'].map(key => [key, byId(`raid-${key}`).checked])),
      },
    };
    if (byId('mod-enabled').checked && !Object.values(protections).some(protection => protection.enabled)) throw new Error('Enable at least one Raid Protection defense.');
    if (protections.destructive_actions.enabled && !['watch_bans', 'watch_kicks', 'watch_channels', 'watch_roles', 'watch_webhooks'].some(key => protections.destructive_actions[key])) throw new Error('Choose at least one destructive staff action to watch.');
    const staffAction = byId('raid-staff-action').value;
    const dangerousStaffResponse = byId('mod-enabled').checked && byId('raid-mode').value === 'enforce' && protections.destructive_actions.enabled && ['kick', 'ban'].includes(staffAction);
    if (dangerousStaffResponse && !byId('raid-danger-confirm').checked) throw new Error(`Confirm the ${staffAction === 'kick' ? 'Kick' : 'Ban'} staff response before saving.`);
    return {
      enabled: byId('mod-enabled').checked,
      schema_version: 2,
      mode: byId('raid-mode').value,
      protections,
      response: {
        join_action: byId('raid-join-action').value,
        staff_action: staffAction,
        lock_minutes: number('raid-lock-minutes'),
      },
      alert_channel_id: selectedChannel(byId('raid-alert-channel').value) || null,
      trusted_roles: [...new Set([...selectedValues(byId('raid-trusted-roles')), ...splitLines(byId('raid-unresolved-roles').value)])],
      trusted_members: splitLines(byId('raid-trusted-members').value),
      confirm_staff_action: dangerousStaffResponse ? byId('raid-danger-confirm').checked : false,
    };
  }
  if (name === 'welcome' || name === 'leave') {
    const payload = {
      enabled: byId('mod-enabled').checked,
      channel_id: selectedChannel(byId('mod-channel').value) || null,
      message: byId('mod-message').value,
      use_images: byId('mod-use-images').checked,
      image_urls: splitLines(byId('mod-images').value),
    };
    if (name === 'welcome') payload.wait_for_verify = byId('mod-wait-verify').checked;
    return payload;
  }
  if (name === 'restore_roles') {
    return { enabled: byId('mod-enabled').checked, allow_permissions: byId('mod-perms').checked };
  }
  if (name === 'selfroles') {
    const menuId = byId('selfrole-menu-picker').value;
    return {
      action: menuId === '__new' ? 'create' : 'update',
      menu_id: menuId === '__new' ? null : menuId,
      channel_id: selectedChannel(byId('mod-channel').value) || null,
      role_ids: selectedValues(byId('mod-roles')),
      text: byId('mod-message').value,
      label: 'Select your role',
      style: byId('mod-style').value,
      limit: Number(byId('mod-limit').value),
      show_roles: byId('mod-show-roles').checked,
      show_percentage: byId('mod-show-percentage').checked,
    };
  }
  if (name === 'starboard') {
    const boards = [...document.querySelectorAll('.starboard-row')].map((row, index) => {
      const name = row.querySelector('.starboard-name').value.trim();
      const channelId = selectedChannel(row.querySelector('.starboard-channel').value) || null;
      const emoji = row.querySelector('.starboard-emoji').value.trim();
      const threshold = Number(row.querySelector('.starboard-threshold').value);
      if (!name) throw new Error(`Give starboard ${index + 1} a name.`);
      if (!channelId) throw new Error(`Choose a destination channel for ${name}.`);
      if (!emoji) throw new Error(`Choose a reaction emoji for ${name}.`);
      if (!Number.isInteger(threshold) || threshold < 1 || threshold > 100) throw new Error(`${name} must require between 1 and 100 reactions.`);
      return {
        board_id: row.dataset.boardId || null,
        name,
        channel_id: channelId,
        emoji,
        threshold,
        enabled: row.querySelector('.starboard-enabled-input').checked,
      };
    });
    const foldedNames = boards.map(board => board.name.toLocaleLowerCase());
    if (new Set(foldedNames).size !== boards.length) throw new Error('Every starboard needs a unique name.');
    return { boards };
  }
  return {
    enabled: byId('mod-enabled').checked,
    channel_id: selectedChannel(byId('mod-channel').value) || null,
    keep_clean: byId('mod-clean').checked,
  };
}

async function saveModule(name, payload, successMessage) {
  const saveButton = byId('module-save');
  saveButton.disabled = true;
  try {
    if (name === 'verification') {
      Object.assign(state.settings.verification, payload);
      const response = await api(`/api/guilds/${state.guildId}/settings?refresh=verification`, {
        method: 'PUT',
        body: JSON.stringify(state.settings),
      });
      state.settings = response.settings;
      syncVerificationModule();
      renderSettings();
      renderModules();
      showToast(successMessage || (payload.enabled ? 'Verification settings saved.' : 'Verification turned off and its message removed.'));
      return true;
    }
    const response = await api(`/api/guilds/${state.guildId}/modules/${name}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    state.modules[name] = response.settings;
    renderModules();
    renderOverviewModules();
    showToast(successMessage || `${moduleMeta[name].title} settings saved.`);
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  } finally {
    saveButton.disabled = false;
  }
}

function openQuickEditor(type) {
  if (type === 'verification') {
    openModule('verification');
    return;
  }
  state.quickEdit = type;
  const body = byId('quick-edit-body');
  const title = byId('quick-edit-title');
  if (type === 'prefix') {
    title.textContent = 'Command prefix';
    body.innerHTML = `<label class="dialog-setting dialog-setting-stack"><span><strong>Prefix</strong><small>Use one to five characters with no spaces.</small></span><input id="quick-prefix" maxlength="5" required></label>${toggleSetting('quick-personal', 'Personal prefixes', 'Let members carry a personal prefix across servers.')}`;
    byId('quick-prefix').value = state.settings.prefix.value;
    setEnabled('quick-personal', state.settings.prefix.allow_personal);
  } else if (type === 'purge') {
    title.textContent = 'Bulk delete limit';
    body.innerHTML = '<label class="dialog-setting dialog-setting-stack"><span><strong>Messages to delete at once</strong><small>Moderators cannot remove more than this with one command.</small></span><input id="quick-purge" type="number" min="1" max="3000" required></label>';
    byId('quick-purge').value = state.settings.general.purge_limit;
  } else if (type === 'xp') {
    title.textContent = 'XP reward range';
    body.innerHTML = '<div class="dialog-split"><label class="dialog-setting dialog-setting-stack"><span><strong>Minimum XP</strong><small>Lowest reward per eligible message.</small></span><input id="quick-min-xp" type="number" min="0" max="100" required></label><label class="dialog-setting dialog-setting-stack"><span><strong>Maximum XP</strong><small>Highest reward per eligible message.</small></span><input id="quick-max-xp" type="number" min="0" max="100" required></label></div>';
    byId('quick-min-xp').value = state.settings.ranking.min_xp_per_msg;
    byId('quick-max-xp').value = state.settings.ranking.max_xp_per_msg;
  }
  openDialog('quick-edit-dialog');
}

function renderSettings() {
  const settings = state.settings;
  byId('prefix-input').value = settings.prefix.value;
  byId('personal-prefixes').checked = settings.prefix.allow_personal;
  byId('language-select').value = settings.general.language || 'en';
  byId('purge-limit').value = settings.general.purge_limit;
  byId('purge-confirmation').checked = settings.general.purge_confirmation === true;
  channelOptions(byId('warn-channel'), settings.general.warns_channel);
  channelOptions(byId('level-channel'), settings.messages.level_up_messages, { current: true });
  channelOptions(byId('moderation-channel'), settings.messages.redirect_mod_commands);

  renderChannelChips('disabled-channels', settings.general.disabled_channels);

  const ranking = settings.ranking;
  byId('min-xp').value = ranking.min_xp_per_msg;
  byId('max-xp').value = ranking.max_xp_per_msg;
  byId('first-level-xp').value = ranking.first_lvl_xp_req;
  byId('timeframe').value = ranking.timeframe;
  byId('message-limit').value = ranking.msgs_within_timeframe;
  renderChannelChips('xp-disabled-channels', ranking.disabled_channels);

  syncVerificationModule();
  renderOverviewStats();
}

function renderOverviewStats() {
  if (!state.settings) return;
  byId('overview-prefix').textContent = state.settings.prefix.value;
  byId('overview-personal').textContent = state.settings.prefix.allow_personal ? 'allowed' : 'disabled';
  byId('overview-purge').textContent = Number(state.settings.general.purge_limit).toLocaleString();
  byId('overview-xp').textContent = `${state.settings.ranking.min_xp_per_msg}–${state.settings.ranking.max_xp_per_msg}`;
  const verification = state.settings.verification;
  byId('overview-verification').textContent = verification.enabled ? 'ONLINE' : 'OFFLINE';
  byId('overview-verification-detail').textContent = verification.enabled
    ? verification.panel_active ? 'verification message active' : 'save to post its message'
    : 'verification is turned off';
  document.querySelector('.verification-stat').classList.toggle('online', verification.enabled);
  renderOverviewModules();
}

function safeBackground(value) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
  } catch { return null; }
}

function renderRankCard() {
  if (!state.profile) return;
  const card = byId('web-rank-card');
  const rank = state.profile.ranks[state.rankScope];
  const isGlobal = state.rankScope === 'global';
  byId('card-rank').textContent = rank.rank ? `RANK #${Number(rank.rank).toLocaleString()}` : 'UNRANKED';
  byId('card-level').textContent = Number(rank.level).toLocaleString();
  byId('card-name').textContent = state.profile.user.display_name;
  byId('card-title').textContent = state.profile.title;
  byId('card-avatar').src = state.profile.user.avatar;
  byId('card-location').textContent = isGlobal ? 'GLOBAL FATE NETWORK' : state.profile.guild.name.toUpperCase();
  byId('card-progress-text').textContent = `${Number(rank.progress).toLocaleString()} / ${Number(rank.next_level_xp).toLocaleString()} XP`;
  byId('card-required').textContent = `${Number(rank.required).toLocaleString()} until next level`;
  card.style.setProperty('--rank-progress', `${rank.percent}%`);
  const background = safeBackground(state.profile.background);
  card.classList.toggle('has-background', Boolean(background));
  if (background) card.style.setProperty('--rank-background', `url("${background.replace(/["\\]/g, '')}")`);
}

function renderOwnerSettings(payload) {
  const settings = payload.settings;
  state.ownerSettings = settings;
  state.ownerSettingsRevision = payload.revision;
  state.ownerSettingsLoaded = true;
  byId('owner-activity-status').value = settings.activity_status;
  byId('owner-theme-color').value = settings.theme_color;
  byId('owner-theme-color-value').textContent = settings.theme_color.toUpperCase();
  byId('owner-debug-mode').checked = settings.debug_mode;
  byId('owner-debug-logging').checked = settings.debug_logging;
  byId('owner-max-cached-messages').value = settings.max_cached_messages;
}

async function loadOwnerSettings({ force = false } = {}) {
  if (!state.session?.is_owner || state.ownerSettingsLoading || (state.ownerSettingsLoaded && !force)) return;
  state.ownerSettingsLoading = true;
  const saveButton = byId('owner-save');
  saveButton.disabled = true;
  byId('owner-note').textContent = 'Loading Fate-wide settings…';
  try {
    const payload = await api('/api/owner/settings');
    renderOwnerSettings(payload);
    byId('owner-note').textContent = 'Live settings apply now. Cache capacity applies after a restart.';
  } catch (error) {
    if ([401, 403].includes(error.status)) {
      clearSensitiveDashboardState();
      location.reload();
      return;
    }
    byId('owner-note').textContent = error.message;
    showToast(error.message, true);
  } finally {
    state.ownerSettingsLoading = false;
    saveButton.disabled = false;
  }
}

function isPanelAvailable(panel) {
  const button = document.querySelector(`.control-nav button[data-panel="${panel}"]`);
  if (!button || button.hidden) return false;
  return panel !== 'owner' || Boolean(state.session?.is_owner);
}

function switchPanel(panel) {
  if (!isPanelAvailable(panel)) {
    panel = !state.guildId && isPanelAvailable('owner') ? 'owner' : 'overview';
  }
  document.querySelectorAll('.control-nav button').forEach(button => button.classList.toggle('active', button.dataset.panel === panel));
  document.querySelectorAll('.control-panel').forEach(section => section.classList.toggle('active', section.dataset.panelContent === panel));
  byId('panel-title').textContent = panel.charAt(0).toUpperCase() + panel.slice(1);
  const nextUrl = panel === 'overview' ? location.pathname : `${location.pathname}#${panel}`;
  history.replaceState(null, '', nextUrl);
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (panel === 'leaderboards') loadLeaderboard();
  if (panel === 'owner') {
    loadingState.hidden = true;
    controlContent.hidden = false;
    loadOwnerSettings();
    loadOwnerMemory();
    startOwnerMemoryRefresh();
  } else if (state.guildId && !state.settings) {
    stopOwnerMemoryRefresh();
    loadGuild();
  } else {
    stopOwnerMemoryRefresh();
  }
}

async function loadGuild() {
  state.activityLogs = emptyActivityLogState();
  loadingState.hidden = false;
  controlContent.hidden = true;
  try {
    const [settingsPayload, profilePayload, modulesPayload] = await Promise.all([
      api(`/api/guilds/${state.guildId}/settings`),
      api(`/api/guilds/${state.guildId}/profile`),
      api(`/api/guilds/${state.guildId}/modules`),
    ]);
    state.settings = settingsPayload.settings;
    state.channels = settingsPayload.channels;
    state.roles = settingsPayload.roles;
    state.profile = profilePayload;
    state.modules = modulesPayload.modules;
    state.modulesLive = modulesPayload.live;
    const guild = state.guilds.find(item => item.id === state.guildId);
    byId('server-avatar').textContent = initials(guild.name);
    byId('welcome-heading').textContent = `${guild.name} is ready.`;
    renderSettings();
    renderRankCard();
    renderModules();
    loadingState.hidden = true;
    controlContent.hidden = false;
    const hash = location.hash.slice(1);
    const requestedPanel = hash === 'rankings' ? 'leaderboards' : hash;
    const requested = isPanelAvailable(requestedPanel) ? requestedPanel : 'overview';
    switchPanel(requested);
  } catch (error) {
    loadingState.querySelector('p').textContent = error.message;
    showToast(error.message, true);
  }
}

async function saveAllSettings(successMessage, { refreshVerification = false } = {}) {
  try {
    const suffix = refreshVerification ? '?refresh=verification' : '';
    const payload = await api(`/api/guilds/${state.guildId}/settings${suffix}`, {
      method: 'PUT',
      body: JSON.stringify(state.settings),
    });
    state.settings = payload.settings;
    renderSettings();
    showToast(successMessage);
    return true;
  } catch (error) {
    showToast(error.message, true);
    return false;
  }
}

byId('general-form').addEventListener('submit', event => {
  event.preventDefault();
  state.settings.prefix.value = byId('prefix-input').value.trim();
  state.settings.prefix.allow_personal = byId('personal-prefixes').checked;
  state.settings.general.language = byId('language-select').value;
  state.settings.general.purge_limit = Number(byId('purge-limit').value);
  state.settings.general.purge_confirmation = byId('purge-confirmation').checked;
  state.settings.general.warns_channel = selectedChannel(byId('warn-channel').value) || null;
  state.settings.general.disabled_channels = [...document.querySelectorAll('#disabled-channels input:checked')].map(input => input.value);
  saveAllSettings('General settings saved.');
});

byId('ranking-form').addEventListener('submit', async event => {
  event.preventDefault();
  Object.assign(state.settings.ranking, {
    min_xp_per_msg: Number(byId('min-xp').value),
    max_xp_per_msg: Number(byId('max-xp').value),
    first_lvl_xp_req: Number(byId('first-level-xp').value),
    timeframe: Number(byId('timeframe').value),
    msgs_within_timeframe: Number(byId('message-limit').value),
    disabled_channels: [...document.querySelectorAll('#xp-disabled-channels input:checked')].map(input => input.value),
  });
  await saveAllSettings('Ranking settings saved.');
  try {
    state.profile = await api(`/api/guilds/${state.guildId}/profile`);
    renderRankCard();
  } catch { /* Saving succeeded; the profile can refresh later. */ }
});

byId('messages-form').addEventListener('submit', event => {
  event.preventDefault();
  state.settings.messages.level_up_messages = selectedChannel(byId('level-channel').value);
  state.settings.messages.redirect_mod_commands = selectedChannel(byId('moderation-channel').value);
  saveAllSettings('Message settings saved.');
});

document.querySelectorAll('[data-quick-edit]').forEach(card => card.addEventListener('click', () => openQuickEditor(card.dataset.quickEdit)));

byId('quick-edit-form').addEventListener('submit', async event => {
  event.preventDefault();
  const type = state.quickEdit;
  let refreshVerification = false;
  let message = 'Setting saved.';
  if (type === 'prefix') {
    state.settings.prefix.value = byId('quick-prefix').value.trim();
    state.settings.prefix.allow_personal = byId('quick-personal').checked;
    message = 'Command prefix updated.';
  } else if (type === 'purge') {
    state.settings.general.purge_limit = Number(byId('quick-purge').value);
    message = 'Purge safety limit updated.';
  } else if (type === 'xp') {
    state.settings.ranking.min_xp_per_msg = Number(byId('quick-min-xp').value);
    state.settings.ranking.max_xp_per_msg = Number(byId('quick-max-xp').value);
    message = 'XP reward range updated.';
  }
  if (await saveAllSettings(message, { refreshVerification })) closeDialog('quick-edit-dialog');
});

byId('module-form').addEventListener('submit', async event => {
  event.preventDefault();
  const name = state.editingModule;
  if (!name) return;
  let payload;
  try {
    payload = collectModuleSettings(name);
  } catch (error) {
    showToast(error.message, true);
    return;
  }
  if (await saveModule(name, payload)) {
    closeDialog('module-dialog');
    revealModuleCard(name);
  }
});

byId('module-delete').addEventListener('click', async () => {
  if (state.editingModule !== 'selfroles') return;
  const menuId = byId('selfrole-menu-picker').value;
  if (menuId === '__new') return;
  requestConfirmation({
    title: 'Delete this self-role menu?',
    copy: 'This removes the role menu from Discord and cannot be undone here.',
    confirmLabel: 'Delete menu',
    action: async () => {
      if (await saveModule('selfroles', { action: 'delete', menu_id: menuId }, 'Self-role menu deleted.')) closeDialog('module-dialog');
    },
  });
});

byId('confirmation-form').addEventListener('submit', async event => {
  event.preventDefault();
  const action = pendingConfirmation;
  pendingConfirmation = null;
  closeDialog('confirmation-dialog');
  if (action) await action();
});

document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.closeDialog === 'confirmation-dialog') cancelConfirmation();
  else closeDialog(button.dataset.closeDialog);
}));

document.querySelectorAll('.dashboard-dialog').forEach(dialog => dialog.addEventListener('click', event => {
  if (event.target !== dialog) return;
  if (dialog.id === 'confirmation-dialog') cancelConfirmation();
  else closeDialog(dialog.id);
}));

document.addEventListener('keydown', event => {
  const activeDialog = [...document.querySelectorAll('.dashboard-dialog')].filter(dialog => !dialog.hidden).at(-1);
  if (!activeDialog) return;
  if (event.key === 'Escape') {
    if (activeDialog.id === 'confirmation-dialog') cancelConfirmation();
    else closeDialog(activeDialog.id);
    return;
  }
  if (event.key !== 'Tab') return;
  const controls = [...activeDialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
    .filter(control => control.offsetParent !== null);
  if (!controls.length) return;
  const first = controls[0];
  const last = controls.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

document.querySelectorAll('.control-nav button').forEach(button => {
  button.title = button.textContent.trim();
  button.addEventListener('click', () => switchPanel(button.dataset.panel));
});
byId('open-modules-workspace').addEventListener('click', () => switchPanel('modules'));
byId('owner-memory-refresh').addEventListener('click', () => loadOwnerMemory({ feedback: true }));

for (const input of byId('owner-form').querySelectorAll('input')) {
  input.addEventListener('input', () => {
    if (input.id === 'owner-theme-color') {
      byId('owner-theme-color-value').textContent = input.value.toUpperCase();
    }
    if (state.ownerSettingsLoaded) byId('owner-note').textContent = 'Unsaved Fate-wide changes.';
  });
}

byId('owner-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!state.session?.is_owner) return;
  const previousCacheSize = state.ownerSettings?.max_cached_messages;
  const saveButton = byId('owner-save');
  saveButton.disabled = true;
  byId('owner-note').textContent = 'Validating and saving owner settings…';
  try {
    const payload = await api('/api/owner/settings', {
      method: 'PUT',
      body: JSON.stringify({
        settings: {
          activity_status: byId('owner-activity-status').value.trim(),
          theme_color: byId('owner-theme-color').value.toUpperCase(),
          debug_mode: byId('owner-debug-mode').checked,
          debug_logging: byId('owner-debug-logging').checked,
          max_cached_messages: Number(byId('owner-max-cached-messages').value),
        },
        revision: state.ownerSettingsRevision,
      }),
    });
    renderOwnerSettings(payload);
    const cacheChanged = payload.restart_required
      || (previousCacheSize != null && previousCacheSize !== payload.settings.max_cached_messages);
    const presenceWarning = payload.presence_synced === false;
    byId('owner-note').textContent = presenceWarning
      ? 'Saved to configuration, but Discord presence could not update. Fate will retry after restart.'
      : cacheChanged
        ? 'Saved. Restart Fate to apply the new message cache capacity.'
        : 'Saved. Fate’s live owner settings are up to date.';
    showToast(
      presenceWarning
        ? 'Owner settings saved with a Discord presence warning.'
        : cacheChanged
          ? 'Owner settings saved — restart required for cache changes.'
          : 'Owner settings saved.',
      presenceWarning,
    );
  } catch (error) {
    if (error.status === 409) {
      await loadOwnerSettings({ force: true });
      if (!state.session) return;
      byId('owner-note').textContent = 'Settings changed elsewhere, so the latest values were reloaded. Review and save again.';
      showToast('Owner settings changed elsewhere and were reloaded.', true);
      return;
    }
    if ([401, 403].includes(error.status)) {
      clearSensitiveDashboardState();
      location.reload();
      return;
    }
    byId('owner-note').textContent = error.message;
    showToast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

document.querySelectorAll('#rank-scope button').forEach(button => button.addEventListener('click', () => {
  state.rankScope = button.dataset.scope;
  document.querySelectorAll('#rank-scope button').forEach(item => item.classList.toggle('active', item === button));
  renderRankCard();
}));

byId('server-picker').addEventListener('change', event => {
  state.guildId = event.target.value;
  byId('mobile-server-picker').value = state.guildId;
  loadGuild();
});

byId('mobile-server-picker').addEventListener('change', event => {
  state.guildId = event.target.value;
  byId('server-picker').value = state.guildId;
  loadGuild();
});

byId('board-picker').addEventListener('change', event => {
  state.board = event.target.value;
  loadLeaderboard();
});

function avatarNode(entry) {
  if (entry.avatar) {
    const image = document.createElement('img');
    image.src = entry.avatar;
    image.alt = '';
    return image;
  }
  const fallback = document.createElement('span');
  fallback.className = 'avatar-fallback';
  fallback.textContent = initials(entry.name || entry.id);
  return fallback;
}

function renderLeaderboard(payload) {
  byId('board-title').textContent = payload.label;
  const entries = payload.entries;
  const podium = byId('podium');
  const list = byId('leaderboard-list');
  const empty = byId('empty-board');
  podium.replaceChildren();
  list.replaceChildren();
  empty.hidden = entries.length > 0;
  podium.hidden = entries.length === 0;
  list.hidden = entries.length === 0;

  for (const entry of entries.slice(0, 3)) {
    const item = document.createElement('article');
    item.className = `podium-entry ${entry.rank === 1 ? 'first' : entry.rank === 2 ? 'second' : 'third'}`;
    const avatar = document.createElement('div');
    avatar.className = 'podium-avatar';
    avatar.append(avatarNode(entry));
    const badge = document.createElement('span');
    badge.className = 'podium-rank';
    badge.textContent = `#${entry.rank}`;
    avatar.append(badge);
    const name = document.createElement('h3');
    name.textContent = entry.name || entry.id;
    const score = document.createElement('p');
    score.textContent = formatScore(entry.score, payload.board);
    item.append(avatar, name, score);
    podium.append(item);
  }

  for (const entry of entries.slice(3)) {
    const row = document.createElement('article');
    row.className = 'board-row';
    const rank = document.createElement('span');
    rank.textContent = `#${String(entry.rank).padStart(2, '0')}`;
    const person = document.createElement('div');
    person.className = 'board-person';
    person.append(avatarNode(entry));
    const name = document.createElement('strong');
    name.textContent = entry.name || entry.id;
    person.append(name);
    const score = document.createElement('span');
    score.className = 'board-score';
    score.textContent = formatScore(entry.score, payload.board);
    row.append(rank, person, score);
    list.append(row);
  }
}

async function loadLeaderboard() {
  if (!state.guildId) return;
  byId('board-title').textContent = 'Reading deep-space signals…';
  try {
    const payload = await api(`/api/guilds/${state.guildId}/leaderboard?board=${encodeURIComponent(state.board)}`);
    renderLeaderboard(payload);
  } catch (error) {
    byId('board-title').textContent = 'Signal unavailable';
    showToast(error.message, true);
  }
}

byId('logout-button').addEventListener('click', async () => {
  try {
    await api('/auth/logout', { method: 'POST' });
    dashboardAuthChannel?.postMessage({ type: 'logout' });
    clearSensitiveDashboardState();
    location.replace('/');
  } catch (error) { showToast(error.message, true); }
});

async function initialize() {
  try {
    state.session = await api('/api/session');
    if (!state.session.authenticated) {
      authGate.hidden = false;
      if (state.session.dev_mode) byId('dev-login').hidden = false;
      if (!state.session.auth_available && !state.session.dev_mode) {
        byId('discord-login').textContent = 'Discord sign-in is unavailable';
        byId('discord-login').classList.add('button-secondary');
      }
      return;
    }

    dashboardShell.hidden = false;
    byId('pilot-avatar').src = state.session.user.avatar;
    byId('pilot-name').textContent = state.session.user.display_name;
    const isOwner = Boolean(state.session.is_owner);
    document.body.classList.toggle('owner-session', isOwner);
    byId('owner-nav').hidden = !isOwner;
    byId('owner-panel').hidden = !isOwner;
    byId('pilot-role').textContent = isOwner ? 'Bot owner' : 'Server manager';
    const guildPayload = await api('/api/guilds');
    state.guilds = guildPayload.guilds;
    if (!state.guilds.length) {
      if (isOwner) {
        document.querySelectorAll('.control-nav button:not([data-panel="owner"])').forEach(button => { button.hidden = true; });
        document.querySelector('.server-picker-wrap').hidden = true;
        byId('mobile-server-picker').closest('.mobile-server-control').hidden = true;
        loadingState.hidden = true;
        controlContent.hidden = false;
        await loadOwnerSettings();
        switchPanel('owner');
        return;
      }
      loadingState.querySelector('p').textContent = 'No servers with Manage Server permission were found.';
      return;
    }
    const picker = byId('server-picker');
    const mobilePicker = byId('mobile-server-picker');
    for (const guild of state.guilds) {
      picker.add(new Option(guild.name, guild.id));
      mobilePicker.add(new Option(guild.name, guild.id));
    }
    state.guildId = state.guilds[0].id;
    picker.value = state.guildId;
    mobilePicker.value = state.guildId;
    if (isOwner && location.hash.slice(1) === 'owner') {
      loadingState.hidden = true;
      controlContent.hidden = false;
      switchPanel('owner');
      return;
    }
    await loadGuild();
  } catch (error) {
    authGate.hidden = false;
    showToast(error.message, true);
  }
}

initialize();
