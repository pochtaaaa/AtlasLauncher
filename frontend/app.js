// ===== STATE =====
let allVersions = [];
let installedVersions = [];
let currentTab = 'all';
let downloadWatchers = {};
let globalPollInterval = null;
let drawerVersionId = null;
let drawerSettings = {};

// ===== NAVIGATION =====
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const page = item.dataset.page;
        switchPage(page);
        document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
        item.classList.add('active');
    });
});

function switchPage(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${page}`).classList.add('active');
    const titles = { 'play': 'Играть', 'versions': 'Версии', 'settings': 'Настройки' };
    document.getElementById('pageTitle').textContent = titles[page];
    if (page === 'versions') loadVersions();
    else if (page === 'settings') loadSettings();
    else if (page === 'play') { loadPlayVersions(); updateStats(); }
}

// ===== MIRROR =====
async function changeMirror() {
    const mirror = document.getElementById('mirrorSelect').value;
    try {
        await pywebview.api.set_mirror(mirror);
        allVersions = [];
        loadVersions();
        await refreshMirrorInfo();
    } catch (e) { console.error(e); }
}

async function recheckMirrors(evt) {
    const btn = evt ? evt.target : null;
    if (btn) { btn.disabled = true; btn.textContent = '🔄 Проверяем...'; }
    try {
        if (document.getElementById('mirrorSelect').value === 'auto') {
            await pywebview.api.auto_select_mirror();
        } else {
            await pywebview.api.get_mirror_status();
        }
        await refreshMirrorInfo();
    } catch (e) { console.error(e); }
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Перепроверить'; }
}

async function refreshMirrorInfo() {
    try {
        const [mirrorInfo, mirrorStatus] = await Promise.all([
            pywebview.api.get_current_mirror(),
            pywebview.api.get_mirror_status()
        ]);
        const names = {'bmclapi': 'BMCLAPI', 'mcbbs': 'MCBBS', 'mojang': 'Mojang'};
        const select = document.getElementById('mirrorSelect');
        if (select) select.value = mirrorInfo.auto ? 'auto' : mirrorInfo.mirror;

        const hint = document.getElementById('mirrorActiveHint');
        if (hint) {
            const activeLatency = mirrorStatus[mirrorInfo.mirror]?.latency_ms;
            hint.textContent = mirrorInfo.auto
                ? `Авто: ${names[mirrorInfo.mirror] || mirrorInfo.mirror}${activeLatency != null ? ' · ' + activeLatency + ' мс' : ''}`
                : `Вручную: ${names[mirrorInfo.mirror] || mirrorInfo.mirror}`;
        }

        const modeHint = document.getElementById('mirrorModeHint');
        if (modeHint) modeHint.textContent = mirrorInfo.auto ? 'Режим: авто-выбор самого быстрого зеркала' : 'Режим: выбрано вручную';

        const list = document.getElementById('mirrorStatusList');
        if (list) {
            list.innerHTML = Object.entries(mirrorStatus).map(([k, d]) => `
                <div class="mirror-status-item ${k === mirrorInfo.mirror ? 'active' : ''}">
                    <span class="mirror-name">${names[k] || k}${k === mirrorInfo.mirror ? ' <span class=\"mirror-star\">★</span>' : ''}</span>
                    <span class="mirror-state ${d.available ? 'online' : 'offline'}">${d.available ? `✓ ${d.latency_ms != null ? d.latency_ms + ' мс' : 'Доступно'}` : '✗ Недоступно'}</span>
                </div>
            `).join('');
        }
    } catch (e) { console.error(e); }
}

// ===== PLAY PAGE =====
function updateRamDisplay() {
    document.getElementById('ramDisplay').textContent = document.getElementById('ramSlider').value + ' МБ';
}

async function loadPlayVersions() {
    const select = document.getElementById('versionSelect');
    try {
        const r = await pywebview.api.get_installed_versions();
        select.innerHTML = (r.success && r.versions.length)
            ? r.versions.map(v => `<option value="${v}">${v}</option>`).join('')
            : '<option value="">Нет установленных версий</option>';
    } catch (e) { select.innerHTML = '<option value="">Ошибка</option>'; }
}

async function updateStats() {
    try {
        const [v, i, j] = await Promise.all([
            pywebview.api.get_versions().catch(() => ({success:false})),
            pywebview.api.get_installed_versions(),
            pywebview.api.get_java_info()
        ]);
        if (v.success) document.getElementById('statVersions').textContent = v.versions.length;
        if (i.success) document.getElementById('statInstalled').textContent = i.versions.length;
        document.getElementById('statJava').textContent = j.found ? '✓' : '✗';
    } catch (e) { console.error(e); }
}

async function launchGame() {
    const version = document.getElementById('versionSelect').value;
    const username = document.getElementById('usernameInput').value.trim();
    const ram = document.getElementById('ramSlider').value;
    const status = document.getElementById('launchStatus');
    const btn = document.getElementById('launchBtn');
    if (!version) { status.textContent = '❌ Сначала установите версию'; status.className = 'launch-status error'; return; }
    if (!username) { status.textContent = '❌ Введите никнейм'; status.className = 'launch-status error'; return; }
    btn.disabled = true; status.textContent = '🚀 Запуск Minecraft...'; status.className = 'launch-status';
    try {
        const r = await pywebview.api.launch_game(version, username, parseInt(ram));
        if (r.success) {
            status.textContent = '';
            status.className = 'launch-status';
            checkLaunchCrash(version); // если Java упадёт сразу — покажем причину
        } else {
            status.innerHTML = '❌ ' + (r.error || 'Ошибка запуска');
        }
    } catch (e) { status.textContent = '❌ ' + e.message; status.className = 'launch-status error'; }
    btn.disabled = false;
}

async function checkLaunchCrash(version) {
    // Java-процесс, упавший сразу при старте (например, из-за битого профиля загрузчика),
    // обычно успевает написать ошибку в лог за 3-4 секунды
    await new Promise(res => setTimeout(res, 3500));
    try {
        const r = await pywebview.api.get_launch_log(version, 40);
        if (!r.success) return;
        const log = r.log || '';
        const crashed = /Exception in thread|Could not find or load main class|Error: Unable to initialize|FATAL ERROR|A JNI error has occurred/i.test(log);
        if (crashed) {
            const status = document.getElementById('launchStatus');
            status.innerHTML = `❌ Minecraft упал при запуске. <a href="#" onclick="showLaunchLog('${version}');return false;" style="color:var(--accent-light)">Показать лог</a>`;
            status.className = 'launch-status error';
        }
    } catch (e) { /* silent */ }
}

async function showLaunchLog(version) {
    try {
        const r = await pywebview.api.get_launch_log(version, 200);
        const text = r.success ? r.log : (r.error || 'Лог не найден');
        alert(text);
    } catch (e) { alert('Не удалось прочитать лог: ' + e.message); }
}

// ===== VERSIONS PAGE =====
async function loadVersions() {
    const list = document.getElementById('versionsList');
    list.innerHTML = '<div class="loading"><div class="spinner"></div><span>Загрузка версий...</span></div>';
    try {
        const [vr, ir] = await Promise.all([pywebview.api.get_versions(), pywebview.api.get_installed_versions()]);
        if (vr.success) {
            allVersions = vr.versions;
            installedVersions = ir.success ? ir.versions : [];
            renderVersions();
        } else list.innerHTML = `<div class="loading"><span>Ошибка: ${vr.error}</span></div>`;
    } catch (e) { list.innerHTML = `<div class="loading"><span>Ошибка: ${e.message}</span></div>`; }
}

function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    renderVersions();
}
function filterVersions() { renderVersions(); }

function renderVersions() {
    const list = document.getElementById('versionsList');
    const search = document.getElementById('versionSearch').value.toLowerCase();
    const showSnapshots = localStorage.getItem('atlas_show_snapshots') === 'true';
    const showOld = localStorage.getItem('atlas_show_old') === 'true';

    let filtered = allVersions.filter(v => {
        if (v.type === 'release') return true;
        if (v.type === 'snapshot') return showSnapshots;
        if (v.type === 'old_beta' || v.type === 'old_alpha') return showOld;
        return true;
    });

    if (currentTab === 'release') filtered = filtered.filter(v => v.type === 'release');
    else if (currentTab === 'snapshot') filtered = filtered.filter(v => v.type === 'snapshot');
    else if (currentTab === 'old') filtered = filtered.filter(v => v.type === 'old_beta' || v.type === 'old_alpha');
    else if (currentTab === 'installed') filtered = allVersions.filter(v => installedVersions.includes(v.id));
    if (search) filtered = filtered.filter(v => v.id.toLowerCase().includes(search));
    if (filtered.length === 0) { list.innerHTML = '<div class="loading"><span>Ничего не найдено</span></div>'; return; }

    const typeColors = { 'release': 'color: var(--accent-light);', 'snapshot': 'color: var(--warning);', 'old_beta': 'color: var(--danger);', 'old_alpha': 'color: var(--danger);' };
    const typeLabels = { 'release': 'Релиз', 'snapshot': 'Снапшот', 'old_beta': 'Beta', 'old_alpha': 'Alpha' };

    list.innerHTML = filtered.map((v, i) => {
        const isInstalled = installedVersions.includes(v.id);
        const isDownloading = downloadWatchers[v.id] !== undefined;
        // Загружаем тему из localStorage
        const theme = localStorage.getItem(`atlas_theme_${v.id}`) || 'default';
        const themeClass = theme !== 'default' ? `theme-${theme}` : '';

        return `
            <div class="version-item ${isDownloading ? 'downloading' : ''} ${themeClass}" id="ver-${v.id}" style="animation-delay: ${i * 0.02}s" onclick="openVersionDrawer('${v.id}')">
                <div style="flex:1">
                    <div class="version-info">
                        <h4>${v.id}</h4>
                        <span style="${typeColors[v.type] || ''}">${typeLabels[v.type] || v.type}</span>
                    </div>
                    <div class="inline-progress" id="prog-${v.id}" style="display:${isDownloading ? 'block' : 'none'}">
                        <div class="inline-progress-track"><div class="inline-progress-bar" id="bar-${v.id}"></div></div>
                        <div class="inline-progress-info">
                            <span class="pct" id="pct-${v.id}">0%</span>
                            <span class="status" id="st-${v.id}">Подготовка...</span>
                        </div>
                    </div>
                </div>
                <div class="version-actions" onclick="event.stopPropagation()">
                    ${isInstalled
                        ? `<div style="display:flex;gap:6px"><button class="btn-small installed" onclick="playVersion('${v.id}')">▶ Играть</button><button class="btn-small btn-delete" onclick="deleteVersion('${v.id}')">🗑</button></div>`
                        : `<button class="btn-small" id="btn-${v.id}" onclick="downloadVersion('${v.id}')" ${isDownloading ? 'disabled' : ''}>⬇ Установить</button>`
                    }
                </div>
            </div>
        `;
    }).join('');
}

async function downloadVersion(versionId) {
    const btn = document.getElementById(`btn-${versionId}`);
    if (btn) btn.disabled = true;
    const prog = document.getElementById(`prog-${versionId}`);
    const card = document.getElementById(`ver-${versionId}`);
    if (prog) prog.style.display = 'block';
    if (card) card.classList.add('downloading');
    try {
        const r = await pywebview.api.download_version(versionId);
        if (!r.success) {
            showToast(r.error || 'Ошибка запуска загрузки', 'error');
            if (btn) btn.disabled = false;
            if (prog) prog.style.display = 'none';
            if (card) card.classList.remove('downloading');
            return;
        }
        startDownloadWatcher(versionId);
    } catch (e) {
        showToast(e.message, 'error');
        if (btn) btn.disabled = false;
        if (prog) prog.style.display = 'none';
        if (card) card.classList.remove('downloading');
    }
}

function startDownloadWatcher(versionId) {
    if (downloadWatchers[versionId]) return;
    downloadWatchers[versionId] = setInterval(async () => {
        try {
            const info = await pywebview.api.get_download_progress(versionId);
            updateDownloadUI(versionId, info);
            if (info.done) {
                clearInterval(downloadWatchers[versionId]);
                delete downloadWatchers[versionId];
                if (info.error) showToast(`❌ ${versionId}: ${info.error}`, 'error');
                else {
                    showToast(`✅ ${versionId} установлена!`, 'success');
                    installedVersions.push(versionId);
                    renderVersions();
                    loadPlayVersions();
                    updateStats();
                }
            }
        } catch (e) { clearInterval(downloadWatchers[versionId]); delete downloadWatchers[versionId]; }
    }, 500);
}

function updateDownloadUI(versionId, info) {
    const bar = document.getElementById(`bar-${versionId}`);
    const pct = document.getElementById(`pct-${versionId}`);
    const st = document.getElementById(`st-${versionId}`);
    if (bar) bar.style.width = info.progress + '%';
    if (pct) pct.textContent = Math.round(info.progress) + '%';
    if (st) {
        const map = { 'starting': 'Запуск...', 'fetching_manifest': 'Список версий...', 'downloading_json': 'Метаданные...', 'downloading_client': 'Клиент...', 'downloading_libraries': 'Библиотеки...', 'downloading_assets_index': 'Индекс ассетов...', 'downloading_assets': 'Ассеты...', 'extracting_natives': 'Распаковка...', 'completed': 'Готово!' };
        st.textContent = map[info.status] || info.status;
    }
}

function playVersion(versionId) {
    document.getElementById('versionSelect').value = versionId;
    switchPage('play');
    document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelector('[data-page="play"]').classList.add('active');
}

async function deleteVersion(versionId) {
    if (!confirm(`Удалить версию ${versionId}?\nВсе файлы версии будут удалены.`)) return;
    try {
        const r = await pywebview.api.delete_version(versionId);
        if (r.success) {
            showToast(`🗑 ${versionId} удалена`, 'info');
            installedVersions = installedVersions.filter(v => v !== versionId);
            renderVersions();
            loadPlayVersions();
            updateStats();
        } else showToast('❌ ' + (r.error || 'Ошибка удаления'), 'error');
    } catch (e) { showToast('❌ ' + e.message, 'error'); }
}

// ===== DRAWER =====
async function openVersionDrawer(versionId) {
    drawerVersionId = versionId;
    document.getElementById('drawerVersionName').textContent = versionId;
    document.getElementById('drawerOverlay').classList.add('active');
    document.getElementById('versionDrawer').classList.add('active');

    // Загружаем настройки
    try {
        const r = await pywebview.api.get_version_settings(versionId);
        drawerSettings = r.success ? r.settings : {};
    } catch (e) { drawerSettings = {}; }

    // Темы
    renderThemeGrid();

    // Java options
    await loadDrawerJavaOptions();
    const javaSelect = document.getElementById('drawerJavaSelect');
    if (javaSelect) javaSelect.value = drawerSettings.custom_java_path || '';

    // RAM
    const ram = drawerSettings.ram || 2048;
    document.getElementById('drawerRamSlider').value = ram;
    document.getElementById('drawerRamDisplay').textContent = ram + ' МБ';

    // Загрузчик
    await loadDrawerLoaders();

    // Моды
    await loadDrawerMods();
}

function closeDrawer() {
    document.getElementById('drawerOverlay').classList.remove('active');
    document.getElementById('versionDrawer').classList.remove('active');
    drawerVersionId = null;
    drawerSettings = {};
}

function renderThemeGrid() {
    const grid = document.getElementById('themeGrid');
    const themes = ['default', 'red', 'orange', 'green', 'purple', 'pink', 'cyan'];
    const currentTheme = localStorage.getItem(`atlas_theme_${drawerVersionId}`) || 'default';

    grid.innerHTML = themes.map(t => `
        <div class="theme-option ${t === currentTheme ? 'active' : ''}" data-theme="${t}" onclick="setVersionTheme('${t}')"></div>
    `).join('');
}

function setVersionTheme(theme) {
    if (!drawerVersionId) return;
    if (theme === 'default') localStorage.removeItem(`atlas_theme_${drawerVersionId}`);
    else localStorage.setItem(`atlas_theme_${drawerVersionId}`, theme);
    renderThemeGrid();
    renderVersions(); // Перерисовываем список
}

async function loadDrawerJavaOptions() {
    const select = document.getElementById('drawerJavaSelect');
    try {
        const options = await pywebview.api.get_all_java_options();
        select.innerHTML = '<option value="">Авто (по умолчанию)</option>' +
            options.map(o => `<option value="${o.path}">${o.label}</option>`).join('');
    } catch (e) { select.innerHTML = '<option value="">Авто</option>'; }
}

function updateDrawerRamDisplay() {
    document.getElementById('drawerRamDisplay').textContent = document.getElementById('drawerRamSlider').value + ' МБ';
}

async function saveDrawerSetting(key, value) {
    if (!drawerVersionId) return;
    try {
        const settings = {};
        settings[key] = value;
        await pywebview.api.set_version_settings(drawerVersionId, settings);
        drawerSettings[key] = value;
    } catch (e) { console.error(e); }
}

// ===== MOD LOADERS =====
const LOADER_NAMES = { fabric: 'Fabric', forge: 'Forge', neoforge: 'NeoForge', quilt: 'Quilt' };
let loaderData = { fabric: [], forge: [], neoforge: [], quilt: [] };
let selectedLoaderType = null;
let selectedLoaderVersion = null;

async function loadDrawerLoaders() {
    if (!drawerVersionId) return;
    const badge = document.getElementById('loaderBadge');
    const uninstallBtn = document.getElementById('loaderUninstallBtn');

    closeLoaderVersions();
    loaderData = { fabric: [], forge: [], neoforge: [], quilt: [] };
    document.querySelectorAll('.loader-tile').forEach(t => {
        t.classList.add('loading');
        t.classList.remove('installed', 'empty');
        const c = t.querySelector('.loader-tile-count');
        if (c) c.textContent = '…';
    });

    // Проверяем установленный загрузчик
    let installedLoader = null;
    try {
        const installed = await pywebview.api.get_installed_loader(drawerVersionId);
        if (installed.success && installed.loader) {
            installedLoader = installed.loader;
            const name = LOADER_NAMES[installed.loader] || installed.loader;
            badge.textContent = name;
            badge.className = 'loader-badge ' + installed.loader;
            uninstallBtn.style.display = 'inline-block';
        } else {
            badge.textContent = 'Vanilla';
            badge.className = 'loader-badge';
            uninstallBtn.style.display = 'none';
        }
    } catch (e) {
        badge.textContent = 'Vanilla';
        badge.className = 'loader-badge';
        uninstallBtn.style.display = 'none';
    }

    document.querySelectorAll('.loader-tile').forEach(t => {
        t.classList.toggle('installed', t.dataset.loader === installedLoader);
    });

    // Загружаем версии всех загрузчиков
    try {
        const r = await pywebview.api.get_modloaders(drawerVersionId);
        document.querySelectorAll('.loader-tile').forEach(t => t.classList.remove('loading'));
        if (!r.success) return;
        loaderData = r.loaders;

        for (const type of Object.keys(LOADER_NAMES)) {
            const count = (loaderData[type] || []).length;
            const idSuffix = type.charAt(0).toUpperCase() + type.slice(1);
            const countEl = document.getElementById('count' + idSuffix);
            const tile = document.querySelector(`.loader-tile[data-loader="${type}"]`);
            if (countEl) countEl.textContent = count > 0 ? `${count} версий` : 'Недоступно';
            if (tile) tile.classList.toggle('empty', count === 0);
        }
    } catch (e) {
        console.error(e);
        document.querySelectorAll('.loader-tile').forEach(t => t.classList.remove('loading'));
    }
}

function getBestLoaderVersion(type) {
    const list = loaderData[type] || [];
    if (list.length === 0) return null;
    const stable = list.find(v => v.stable);
    return (stable || list[0]).version;
}

function selectLoaderType(type) {
    const list = loaderData[type] || [];
    if (list.length === 0) { showToast('Нет доступных версий для этой версии Minecraft', 'error'); return; }

    selectedLoaderType = type;
    selectedLoaderVersion = null;

    const bestVersion = getBestLoaderVersion(type);
    const panel = document.getElementById('loaderVersions');
    const title = document.getElementById('loaderVersionsTitle');
    const listEl = document.getElementById('loaderVersionList');
    const installBtn = document.getElementById('installLoaderBtn');
    const hint = document.getElementById('loaderHint');

    title.textContent = LOADER_NAMES[type];
    listEl.innerHTML = list.map(v => `
        <div class="loader-version-row" data-version="${v.version}" onclick="selectLoaderVersion(this, '${v.version.replace(/'/g, "\\'")}')">
            <span class="loader-version-num">${v.version}</span>
            <span class="loader-version-tags">
                ${v.version === bestVersion ? '<span class="loader-version-star" title="Рекомендуемая версия">★</span>' : ''}
                ${v.stable === false ? '<span class="loader-version-tag beta">beta</span>' : ''}
            </span>
        </div>
    `).join('');

    document.getElementById('loaderTiles').style.display = 'none';
    panel.style.display = 'flex';
    installBtn.disabled = true;
    hint.textContent = `Выбери версию ${LOADER_NAMES[type]} и нажми «Установить». Звездой отмечена рекомендуемая версия.`;

    if (bestVersion) {
        const bestRow = listEl.querySelector(`.loader-version-row[data-version="${CSS.escape(bestVersion)}"]`);
        if (bestRow) selectLoaderVersion(bestRow, bestVersion);
    }
}

function selectLoaderVersion(rowEl, version) {
    selectedLoaderVersion = version;
    document.querySelectorAll('.loader-version-row').forEach(row => row.classList.remove('selected'));
    if (rowEl) rowEl.classList.add('selected');
    document.getElementById('installLoaderBtn').disabled = false;
}

function closeLoaderVersions() {
    document.getElementById('loaderVersions').style.display = 'none';
    document.getElementById('loaderTiles').style.display = 'grid';
    document.getElementById('loaderHint').textContent = 'Выбери загрузчик модов, затем версию из списка. Звездой отмечена рекомендуемая версия.';
    selectedLoaderType = null;
    selectedLoaderVersion = null;
}

async function installLoader() {
    if (!drawerVersionId) return;
    if (!selectedLoaderType || !selectedLoaderVersion) { showToast('Выбери версию загрузчика', 'error'); return; }

    const progress = document.getElementById('loaderProgress');
    const bar = document.getElementById('loaderProgressBar');
    const text = document.getElementById('loaderProgressText');
    const btn = document.getElementById('installLoaderBtn');

    btn.disabled = true;
    progress.style.display = 'block';
    bar.style.width = '10%';
    text.textContent = 'Скачивание...';

    try {
        const loaderType = selectedLoaderType;
        const loaderVersion = selectedLoaderVersion;
        let r;

        if (loaderType === 'fabric') {
            r = await pywebview.api.install_fabric(drawerVersionId, loaderVersion);
        } else if (loaderType === 'forge') {
            r = await pywebview.api.install_forge(drawerVersionId, loaderVersion);
        } else if (loaderType === 'neoforge') {
            r = await pywebview.api.install_neoforge(drawerVersionId, loaderVersion);
        } else if (loaderType === 'quilt') {
            r = await pywebview.api.install_quilt(drawerVersionId, loaderVersion);
        } else {
            throw new Error('Unknown loader type');
        }

        if (r.success) {
            bar.style.width = '100%';
            text.textContent = 'Готово!';
            const name = LOADER_NAMES[loaderType];
            showToast(`✅ ${name} ${loaderVersion} установлен!`, 'success');
            await loadDrawerLoaders();
        } else {
            text.textContent = 'Ошибка: ' + r.error;
            showToast('❌ ' + r.error, 'error');
        }
    } catch (e) {
        text.textContent = 'Ошибка: ' + e.message;
        showToast('❌ ' + e.message, 'error');
    }

    setTimeout(() => { progress.style.display = 'none'; bar.style.width = '0%'; }, 2000);
    btn.disabled = false;
}

async function uninstallLoader() {
    if (!drawerVersionId) return;
    if (!confirm('Удалить загрузчик и восстановить vanilla версию?')) return;
    try {
        const r = await pywebview.api.uninstall_loader(drawerVersionId);
        if (r.success) {
            showToast('✅ Загрузчик удалён', 'success');
            await loadDrawerLoaders();
        } else {
            showToast('❌ ' + r.error, 'error');
        }
    } catch (e) {
        showToast('❌ ' + e.message, 'error');
    }
}

async function loadDrawerMods() {
    const list = document.getElementById('modList');
    if (!drawerVersionId) return;
    try {
        const r = await pywebview.api.get_version_mods(drawerVersionId);
        if (r.success && r.mods.length) {
            list.innerHTML = r.mods.map(m => `
                <div class="mod-item">
                    <span class="mod-item-name">${m.name}</span>
                    <span class="mod-item-size">${formatBytes(m.size)}</span>
                    <button class="mod-item-delete" onclick="deleteMod('${m.name}')">✕</button>
                </div>
            `).join('');
        } else {
            list.innerHTML = '<p style="color:var(--text-muted);font-size:12px;text-align:center;padding:10px">Нет установленных модов</p>';
        }
    } catch (e) { list.innerHTML = '<p style="color:var(--text-muted);font-size:12px">Ошибка загрузки модов</p>'; }
}

function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/(1024*1024)).toFixed(1) + ' MB';
}

async function deleteMod(modName) {
    if (!drawerVersionId) return;
    try {
        await pywebview.api.delete_mod(drawerVersionId, modName);
        await loadDrawerMods();
        showToast(`🗑 ${modName} удалён`, 'info');
    } catch (e) { showToast('❌ Ошибка удаления мода', 'error'); }
}

// Drag & drop для модов
// ВАЖНО: обычный браузерный File.path в pywebview 6.x не существует — реальный путь к файлу
// pywebview даёт только через свой DOM API (window.dom / events.drop) на стороне Python.
// Поэтому здесь только визуальная реакция, а установка модов при drop происходит в main.py
// (см. _on_mod_files_dropped), который сам вызывает loadDrawerMods() и showToast() после установки.
function handleModDragOver(e) {
    e.preventDefault();
    e.currentTarget.classList.add('dragover');
}
function handleModDragLeave(e) {
    e.currentTarget.classList.remove('dragover');
}
function handleModDropVisual(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('dragover');
}

// Клик по dropzone — открывает системный диалог выбора файла (даёт реальный путь)
async function pickMods() {
    if (!drawerVersionId) { showToast('Сначала открой настройки версии', 'error'); return; }
    const dropzone = document.getElementById('modDropzone');
    if (dropzone) dropzone.classList.add('uploading');
    try {
        const r = await pywebview.api.pick_and_install_mods(drawerVersionId);
        if (r.success) {
            const count = r.installed ? r.installed.length : 0;
            showToast(count > 0 ? `✅ Установлено модов: ${count}` : '❌ Ни один мод не установлен', count > 0 ? 'success' : 'error');
            if (r.errors && r.errors.length) showToast('⚠ ' + r.errors.join('; '), 'error');
            await loadDrawerMods();
        } else if (r.error !== 'Отменено') {
            showToast('❌ ' + r.error, 'error');
        }
    } catch (e) {
        showToast('❌ ' + e.message, 'error');
    }
    if (dropzone) dropzone.classList.remove('uploading');
}

// ===== MRPACK =====
async function pickMrpack() {
    if (!drawerVersionId) { showToast('Сначала открой настройки версии', 'error'); return; }
    const status = document.getElementById('mrpackStatus');
    try {
        const r = await pywebview.api.pick_and_install_mrpack(drawerVersionId);
        if (r.success) {
            status.textContent = '✅ ' + r.message;
            showToast(`✅ Modpack установлен!`, 'success');
            await loadDrawerMods();
        } else if (r.error === 'Отменено') {
            status.textContent = '';
        } else {
            status.textContent = '❌ ' + r.error;
            showToast('❌ ' + r.error, 'error');
        }
    } catch (e) {
        status.textContent = '❌ ' + e.message;
        showToast('❌ Ошибка установки modpack', 'error');
    }
}

// ===== GLOBAL DOWNLOAD POLL =====
function startGlobalPoll() {
    if (globalPollInterval) return;
    globalPollInterval = setInterval(async () => {
        try {
            const active = await pywebview.api.get_active_downloads();
            const container = document.getElementById('activeDownloads');
            const list = document.getElementById('downloadList');
            if (!active || active.length === 0) { container.style.display = 'none'; return; }
            container.style.display = 'block';
            const items = await Promise.all(active.map(async v => {
                const info = await pywebview.api.get_download_progress(v);
                return { id: v, info };
            }));
            list.innerHTML = items.map(it => `
                <div class="download-item">
                    <div class="download-item-name">${it.id}</div>
                    <div class="download-item-track"><div class="download-item-bar" style="width:${it.info.progress}%"></div></div>
                    <div class="download-item-info"><span>${Math.round(it.info.progress)}%</span><span>${it.info.status}</span></div>
                </div>
            `).join('');
        } catch (e) { console.error(e); }
    }, 800);
}

// ===== SETTINGS =====
async function loadSettings() {
    try {
        const javaInfo = await pywebview.api.get_java_info();
        await refreshMirrorInfo();
        updateJavaSidebar(javaInfo);
        loadVersionFilters();
        loadManagedJava();
    } catch (e) { console.error(e); }
}

function updateJavaSidebar(javaInfo) {
    const javaDot = document.querySelector('.java-dot');
    const javaText = document.querySelector('.java-info span');
    if (!javaDot || !javaText) return;
    if (javaInfo.found) {
        javaDot.classList.add('ok');
        javaText.textContent = `Java ${javaInfo.version || ''} найдена`;
    } else {
        javaDot.classList.remove('ok');
        javaText.textContent = 'Java не найдена — скачаем при запуске';
    }
}

async function rescanJava(evt) {
    const btn = evt ? evt.target : null;
    if (btn) { btn.disabled = true; btn.textContent = '🔄 Ищем...'; }
    try {
        await pywebview.api.rescan_java();
        const javaInfo = await pywebview.api.get_java_info();
        updateJavaSidebar(javaInfo);
        await loadManagedJava();
        showToast('✅ Поиск Java обновлён', 'success');
    } catch (e) { showToast('❌ ' + e.message, 'error'); }
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Найти заново'; }
}

async function loadManagedJava() {
    try {
        const [options, sysInfo] = await Promise.all([
            pywebview.api.get_all_java_options(),
            pywebview.api.get_java_info()
        ]);
        const container = document.getElementById('javaSettingsContent');
        let html = '';
        if (sysInfo.found) {
            html += `<p><strong>Рекомендуемая:</strong> Java ${sysInfo.version || '?'} — <span style="opacity:.7">${sysInfo.path}</span></p>`;
        } else {
            html += '<p><strong>Системная Java не найдена</strong></p>';
        }
        if (options && options.length > 1) {
            html += `<p class="setting-hint" style="margin-top:8px">Найдено установок: ${options.length}</p>`;
        } else if (!options || options.length === 0) {
            html += '<p class="setting-hint">При запуске лаунчер автоматически скачает нужную Java</p>';
        }
        container.innerHTML = html;
    } catch (e) { console.error(e); }
}

function loadVersionFilters() {
    const cbSnap = document.getElementById('showSnapshots');
    const cbOld = document.getElementById('showOldVersions');
    if (cbSnap) cbSnap.checked = localStorage.getItem('atlas_show_snapshots') === 'true';
    if (cbOld) cbOld.checked = localStorage.getItem('atlas_show_old') === 'true';
}

function saveVersionFilters() {
    const cbSnap = document.getElementById('showSnapshots');
    const cbOld = document.getElementById('showOldVersions');
    if (cbSnap) localStorage.setItem('atlas_show_snapshots', cbSnap.checked);
    if (cbOld) localStorage.setItem('atlas_show_old', cbOld.checked);
    const versionsPage = document.getElementById('page-versions');
    if (versionsPage && versionsPage.classList.contains('active')) renderVersions();
}

function clearCache() {
    if (confirm('Удалить все библиотеки и ассеты? Версии игры останутся.')) alert('Функция в разработке 😉');
}

// ===== TOASTS =====
function showToast(message, type='info') {
    let container = document.querySelector('.toast-container');
    if (!container) {
        container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => { toast.remove(); }, 4000);
}

// ===== USERNAME =====
document.getElementById('usernameInput').addEventListener('input', (e) => {
    const name = e.target.value.trim() || 'P';
    document.getElementById('userName').textContent = e.target.value.trim() || 'Player';
    document.getElementById('userAvatar').textContent = name[0].toUpperCase();
});

// ===== INIT =====
window.addEventListener('DOMContentLoaded', () => {
    loadPlayVersions();
    loadSettings();
    updateStats();
    startGlobalPoll();
    // Фоновое авто-определение зеркала и Java на старте может занять пару секунд — досверяем результат
    setTimeout(async () => {
        try {
            const javaInfo = await pywebview.api.get_java_info();
            updateJavaSidebar(javaInfo);
            await refreshMirrorInfo();
            updateStats();
        } catch (e) { /* silent */ }
    }, 2500);
});
