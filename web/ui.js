const get = (id) => document.getElementById(id);
const labels = {queued: '排队中', running: '执行中', completed: '已完成', failed: '失败', interrupted: '已中断'};
let active = localStorage.getItem('workbench-session');
let lastDetail = '';
let lastList = '';
let refreshId = 0;

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '请求失败');
  return body;
}

function post(path, body) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
}

function showError(error) {
  get('error').textContent = error ? error.message : '';
  get('error').hidden = !error;
}

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content) node.textContent = content;
  return node;
}

function select(id) {
  if (id !== active) get('message').value = '';
  active = id;
  if (id) localStorage.setItem('workbench-session', id);
  lastDetail = '';
  lastList = '';
  showError(null);
  refresh();
}

function renderDetail(detail) {
  const timeline = get('timeline');
  const atBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  const open = new Set(Array.from(timeline.querySelectorAll('details[open]'), (node) => node.dataset.id));
  get('title').textContent = detail?.title || '工作台';
  get('status').textContent = labels[detail?.runs.at(-1)?.status] || '';
  get('prompt').hidden = !detail;
  get('upload-label').hidden = !detail;
  timeline.replaceChildren();
  if (!detail?.runs.length) {
    const empty = element('div', 'empty');
    empty.append(element('h2', '', detail ? '开始一项工作' : '你的 Agent 工作台'));
    empty.append(element('p', '', detail ? '发送任务，或先上传需要处理的材料。' : '新建会话，开始开发工具或处理材料。'));
    timeline.append(empty);
  }
  for (const run of detail?.runs || []) {
    const turn = element('article', 'turn');
    turn.append(element('p', 'user-message', run.prompt));
    const state = element('div', 'run-state', labels[run.status]);
    if (['failed', 'interrupted'].includes(run.status)) {
      const retry = element('button', '', '重试');
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          await post('/api/sessions/' + detail.id + '/runs', {prompt: run.prompt});
          showError(null);
          await refresh();
        } catch (error) {
          showError(error);
        } finally {
          retry.disabled = false;
        }
      };
      state.append(retry);
    }
    turn.append(state);
    for (const [index, event] of run.events.entries()) {
      if (event.kind === 'reply') {
        turn.append(element('p', 'reply', event.text));
      } else if (event.kind === 'command' || event.kind === 'log') {
        const disclosure = element('details');
        disclosure.dataset.id = run.id + ':' + index;
        disclosure.open = open.has(disclosure.dataset.id);
        disclosure.append(element('summary', '', event.kind === 'command' ? event.text.split('\n')[0] : 'CLI 日志'));
        disclosure.append(element('pre', '', event.text));
        turn.append(disclosure);
      } else {
        turn.append(element('p', event.kind === 'error' ? 'event error' : 'event', event.text));
      }
    }
    timeline.append(turn);
  }
  if (atBottom) timeline.scrollTop = timeline.scrollHeight;
  get('files-empty').hidden = Boolean(detail?.files.length);
  get('files').replaceChildren(...(detail?.files || []).map((name) => {
    const row = element('li');
    const link = element('a', '', name + ' ↓');
    link.href = '/api/sessions/' + detail.id + '/files?name=' + encodeURIComponent(name);
    link.download = '';
    row.append(link);
    return row;
  }));
}

async function refresh() {
  const generation = ++refreshId;
  try {
    const sessions = await api('/api/sessions');
    if (generation !== refreshId) return;
    if (!sessions.some((row) => row.id === active)) active = sessions[0]?.id || null;
    const listKey = JSON.stringify([active, sessions]);
    if (lastList !== listKey) {
      get('sessions').replaceChildren(...sessions.map((row) => {
        const button = element('button', '', row.title + (['queued', 'running'].includes(row.status) ? ' · ' + labels[row.status] : ''));
        button.setAttribute('aria-current', String(active === row.id));
        button.onclick = () => select(row.id);
        return button;
      }));
      lastList = listKey;
    }
    const detail = active ? await api('/api/sessions/' + active) : null;
    if (generation !== refreshId) return;
    const detailKey = JSON.stringify(detail);
    if (lastDetail !== detailKey) {
      renderDetail(detail);
      lastDetail = detailKey;
    }
  } catch (error) {
    if (generation === refreshId) showError(error);
  }
}

get('new-session').onclick = () => get('create').showModal();
get('cancel').onclick = () => get('create').close();
get('session-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const session = await post('/api/sessions', {title: get('session-name').value});
    get('session-name').value = '';
    get('create').close();
    select(session.id);
  } catch (error) {
    get('create').close();
    showError(error);
  } finally {
    button.disabled = false;
  }
};
get('prompt').onsubmit = async (event) => {
  event.preventDefault();
  const sid = active;
  const message = get('message');
  const button = get('prompt').querySelector('button');
  button.disabled = true;
  try {
    await post('/api/sessions/' + sid + '/runs', {prompt: message.value});
    if (sid === active) message.value = '';
    showError(null);
    await refresh();
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
};
get('upload').onchange = async (event) => {
  const sid = active;
  const files = Array.from(event.target.files);
  event.target.disabled = true;
  const errors = [];
  for (const file of files) {
    try {
      await api('/api/sessions/' + sid + '/files?name=' + encodeURIComponent(file.name), {method: 'POST', body: file});
    } catch (error) {
      errors.push(file.name + ' · ' + error.message);
    }
  }
  event.target.value = '';
  event.target.disabled = false;
  showError(errors.length ? new Error(errors.join('；')) : null);
  await refresh();
};
refresh();
setInterval(refresh, 1200);
