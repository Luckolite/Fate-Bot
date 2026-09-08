const commandGrid = document.querySelector('#command-grid');
const commandCount = document.querySelector('#command-count');
const commandTotal = document.querySelector('#command-total');
const searchInput = document.querySelector('#command-search');
const filters = document.querySelector('#category-filters');
const loadMore = document.querySelector('#load-more');

const commandState = {
  commands: [],
  category: 'All',
  query: '',
  visible: 12,
};

function commandMatches(command) {
  const categoryMatch = commandState.category === 'All' || command.category === commandState.category;
  const haystack = `${command.name} ${command.description} ${command.category}`.toLowerCase();
  return categoryMatch && haystack.includes(commandState.query.toLowerCase());
}

function renderCommands() {
  const matches = commandState.commands.filter(commandMatches);
  commandGrid.replaceChildren();
  for (const command of matches.slice(0, commandState.visible)) {
    const article = document.createElement('article');
    article.className = 'command-item';
    const heading = document.createElement('h3');
    heading.textContent = command.name;
    const description = document.createElement('p');
    description.textContent = command.description;
    const category = document.createElement('span');
    category.textContent = command.category;
    article.append(heading, description, category);
    commandGrid.append(article);
  }

  commandCount.textContent = `${matches.length.toLocaleString()} command${matches.length === 1 ? '' : 's'} in range`;
  loadMore.hidden = matches.length <= commandState.visible;
  if (!matches.length) {
    const empty = document.createElement('article');
    empty.className = 'command-item';
    const heading = document.createElement('h3');
    heading.textContent = 'No signal found';
    const description = document.createElement('p');
    description.textContent = 'Try a broader search or a different command category.';
    empty.append(heading, description);
    commandGrid.append(empty);
  }
}

function renderFilters() {
  const categories = ['All', ...new Set(commandState.commands.map(command => command.category))];
  filters.replaceChildren();
  for (const category of categories) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = category === commandState.category ? 'active' : '';
    button.textContent = category;
    button.addEventListener('click', () => {
      commandState.category = category;
      commandState.visible = 12;
      renderFilters();
      renderCommands();
    });
    filters.append(button);
  }
}

async function loadCommands() {
  try {
    const response = await fetch('/api/commands');
    if (!response.ok) throw new Error('Command catalog unavailable');
    const payload = await response.json();
    commandState.commands = payload.commands;
    commandTotal.textContent = `${payload.count}+`;
    renderFilters();
    renderCommands();
  } catch (error) {
    commandCount.textContent = 'Command signal temporarily unavailable';
    renderCommands();
  }
}

searchInput?.addEventListener('input', event => {
  commandState.query = event.target.value.trim();
  commandState.visible = 12;
  renderCommands();
});

loadMore?.addEventListener('click', () => {
  commandState.visible += 18;
  renderCommands();
});

document.addEventListener('keydown', event => {
  if (event.key === '/' && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput?.focus();
  }
});

loadCommands();

