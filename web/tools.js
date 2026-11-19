let publishing = null;
let selectedTool = null;
let outputURL = null;
let modelRevision = 0;

function setStatus(id, message, failed = false) {
  const target = get(id);
  target.textContent = message;
  target.classList.toggle('error', failed);
}

function openPublish(session, source) {
  publishing = {session, source};
  get('publish-form').reset();
  get('publish-source').textContent = source;
  const filename = source.split('/').at(-1);
  get('publish-name').value = filename.replace(/\.py$/, '');
  if (/^json[-_]?(?:2|to)[-_]?csv\.py$/i.test(filename)) {
    get('publish-name').value = 'JSON 转 CSV';
    get('publish-description').value = '将 JSON 转为 CSV，支持预览和下载。';
    get('publish-accept').value = '.json';
    get('publish-output-format').value = 'csv';
  }
  get('publish-error').hidden = true;
  get('publish-tool').showModal();
}

get('publish-cancel').onclick = () => get('publish-tool').close();
get('publish-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const {session, source} = publishing;
    const body = {source, outputFormat: get('publish-output-format').value};
    for (const key of ['name', 'description', 'accept', 'flag', 'flagLabel']) {
      const id = key === 'flagLabel' ? 'flag-label' : key;
      body[key] = get('publish-' + id).value;
    }
    const tool = await post('/api/sessions/' + session + '/tools', body);
    get('publish-tool').close();
    await openTools(tool.id);
  } catch (error) {
    get('publish-error').textContent = error.message;
    get('publish-error').hidden = false;
  } finally {
    button.disabled = false;
  }
};

function renderTool(tool) {
  selectedTool = tool;
  get('tool-panel').hidden = !tool;
  if (!tool) return;
  for (const card of get('tool-list').children) card.setAttribute('aria-current', String(card.dataset.id === tool.id));
  get('tool-name').textContent = tool.name;
  get('tool-description').textContent = tool.description || tool.source;
  get('tool-run').reset();
  get('tool-run-button').disabled = false;
  get('tool-input').accept = tool.accept;
  get('tool-file-name').textContent = '选择输入文件';
  get('tool-file-hint').textContent = (tool.accept ? tool.accept.toUpperCase() + ' · ' : '') + '最大 20 MiB';
  get('tool-flag-row').hidden = !tool.flag;
  get('tool-flag-label').textContent = tool.flag_label;
  get('tool-result').hidden = true;
  setStatus('tool-state', '');
}

async function openTools(preferred = null) {
  switchView('tools');
  try {
    const [tools, candidates] = await Promise.all([api('/api/tools'), api('/api/tools/candidates')]);
    if (currentView !== 'tools') return;
    get('tool-empty').hidden = tools.length > 0;
    get('tool-list').replaceChildren(...tools.map((tool) => {
      const card = element('button', 'tool-card');
      card.dataset.id = tool.id;
      card.append(element('strong', '', tool.name), element('span', '', tool.description || tool.source));
      card.onclick = () => renderTool(tool);
      return card;
    }));
    get('candidate-panel').hidden = candidates.length === 0;
    get('candidate-list').replaceChildren(...candidates.map((candidate) => {
      const card = element('div', 'tool-candidate');
      card.append(element('strong', '', candidate.source), element('span', '', candidate.title));
      const save = element('button', 'publish-file', '配置并保存');
      save.onclick = () => openPublish(candidate.session, candidate.source);
      card.append(save);
      return card;
    }));
    renderTool(tools.find((tool) => tool.id === (preferred || selectedTool?.id)) || tools[0]);
  } catch (error) {
    get('tool-empty').textContent = error.message;
    get('tool-empty').hidden = false;
  }
}

get('show-tools').onclick = () => openTools();
get('tool-input').onchange = () => {
  get('tool-file-name').textContent = get('tool-input').files[0]?.name || '选择输入文件';
};

get('tool-run').onsubmit = async (event) => {
  event.preventDefault();
  const tool = selectedTool;
  const file = get('tool-input').files[0];
  if (!tool || !file) return;
  const button = get('tool-run-button');
  button.disabled = true;
  get('tool-result').hidden = true;
  setStatus('tool-state', '正在运行…');
  try {
    const params = new URLSearchParams({name: file.name, flag: get('tool-flag').checked ? '1' : '0'});
    const result = await api('/api/tools/' + tool.id + '/run?' + params, {method: 'POST', body: file});
    if (tool !== selectedTool) return;
    setStatus('tool-state', result.ok ? '运行完成' : result.error, !result.ok);
    get('tool-result').hidden = false;
    const rows = result.rows || [];
    get('tool-output').hidden = rows.length > 0;
    get('tool-output').textContent = result.output || '脚本没有输出。';
    get('tool-table').hidden = rows.length === 0;
    get('tool-table').querySelector('tbody').replaceChildren(...rows.map((row) => {
      const tr = element('tr');
      tr.append(...row.map((value) => element('td', '', value)));
      return tr;
    }));
    get('tool-diagnostics').hidden = !result.diagnostics;
    get('tool-diagnostics').querySelector('pre').textContent = result.diagnostics || '';
    const link = get('tool-download');
    link.hidden = !result.ok || !result.output;
    if (outputURL) URL.revokeObjectURL(outputURL);
    if (!link.hidden) {
      const isCsv = tool.output_format === 'csv';
      const mime = isCsv ? 'text/csv;charset=utf-8' : 'text/plain;charset=utf-8';
      outputURL = URL.createObjectURL(new Blob([result.output], {type: mime}));
      link.href = outputURL;
      link.download = file.name.replace(/\.[^.]+$/, '') + (isCsv ? '-result.csv' : '-result.txt');
    }
  } catch (error) {
    if (tool === selectedTool) setStatus('tool-state', error.message, true);
  } finally {
    if (tool === selectedTool) button.disabled = false;
  }
};

async function openModel() {
  switchView('model');
  const revision = ++modelRevision;
  setStatus('model-message', '正在读取配置…');
  get('model-save').disabled = true;
  get('model-test').disabled = true;
  try {
    const model = await api('/api/model');
    if (revision !== modelRevision || currentView !== 'model') return;
    get('model-url').value = model.url;
    get('model-name').value = model.model;
    get('model-key').value = '';
    get('model-key-status').textContent = model.hasKey ? '已配置，留空保留' : '未配置';
    setStatus('model-message', '');
    get('model-save').disabled = false;
    get('model-test').disabled = false;
  } catch (error) {
    if (revision === modelRevision) setStatus('model-message', error.message, true);
  }
}

async function updateModel(test) {
  if (!get('model-form').reportValidity()) return;
  const revision = ++modelRevision;
  get('model-save').disabled = true;
  get('model-test').disabled = true;
  setStatus('model-message', test ? '正在查询模型列表…' : '正在保存…');
  try {
    const result = await post('/api/model' + (test ? '/test' : ''), {
      url: get('model-url').value,
      model: get('model-name').value,
      key: get('model-key').value,
    });
    if (revision !== modelRevision) return;
    if (!test) {
      get('model-key').value = '';
      get('model-key-status').textContent = result.hasKey ? '已配置，留空保留' : '未配置';
    }
    setStatus('model-message', test ? '连接成功，模型可用 · ' + result.model : '配置已保存');
  } catch (error) {
    if (revision === modelRevision) setStatus('model-message', error.message, true);
  } finally {
    if (revision === modelRevision) {
      get('model-save').disabled = false;
      get('model-test').disabled = false;
    }
  }
}

get('show-model').onclick = openModel;
get('model-test').onclick = () => updateModel(true);
get('model-form').onsubmit = (event) => { event.preventDefault(); updateModel(false); };
