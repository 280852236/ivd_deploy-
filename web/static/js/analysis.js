
let CSRF_TOKEN = '';
fetch('/api/csrf-token').then(r=>r.json()).then(d=>{CSRF_TOKEN=d.csrf_token;}).catch(()=>{});
const ANALYSIS_ID = window.ANALYSIS_ID;
let _embeddedData = null;
try { _embeddedData = window.EMBEDDED_DATA; } catch(e) {}
const _fileCacheMap = new Map();
const FILE_CACHE_MAX = 15;
const MAX_MODAL_DISPLAY = 5000;
const MAX_SEARCH_DISPLAY = 1000;
function fileCacheGet(key) {
    if (!_fileCacheMap.has(key)) return undefined;
    const v = _fileCacheMap.get(key);
    _fileCacheMap.delete(key);
    _fileCacheMap.set(key, v);
    return v;
}
function fileCacheSet(key, value) {
    if (_fileCacheMap.has(key)) _fileCacheMap.delete(key);
    _fileCacheMap.set(key, value);
    while (_fileCacheMap.size > FILE_CACHE_MAX) {
        _fileCacheMap.delete(_fileCacheMap.keys().next().value);
    }
}
let allDateGroups = [];
let currentFileName = null;
let searchQuery = '';
let hasMoreFiles = false;
let zipTotalCandidates = 0;
let zipProcessed = 0;
let isLoadingMoreFiles = false;

function fetchWithTimeout(url, options = {}, timeout = 300000) {
    const controller = new AbortController();
    const signal = controller.signal;
    const fetchOptions = { ...options, signal };
    const timeoutId = setTimeout(() => {
        controller.abort();
    }, timeout);
    return fetch(url, fetchOptions)
        .finally(() => clearTimeout(timeoutId))
        .catch(err => {
            if (err.name === 'AbortError') {
                const timeoutErr = new Error('请求超时');
                timeoutErr.name = 'TimeoutError';
                throw timeoutErr;
            }
            throw err;
        });
}

// 快速上传 - 系列型号联动
document.getElementById('quickSeries').addEventListener('change', async function() {
    const series = this.value;
    const modelSelect = document.getElementById('quickModel');
    if (!series) {
        modelSelect.innerHTML = '<option value="" style="background:#1e40af;color:white;">选择型号</option>';
        return;
    }
    try {
        const resp = await fetch(`/api/models?series=${series}`);
        const models = await resp.json();
        let opts = '<option value="" style="background:#1e40af;color:white;">选择型号</option>';
        models.forEach(m => opts += `<option value="${m.name}" style="background:#1e40af;color:white;">${m.name}</option>`);
        modelSelect.innerHTML = opts;
        if (models.length > 0) modelSelect.value = models[0].name;
    } catch (e) {
        modelSelect.innerHTML = '<option value="" style="background:#1e40af;color:white;">加载失败</option>';
    }
});

// 快速上传功能 - 直接刷新当前页面显示新结果
document.getElementById('quickFile').addEventListener('change', async function(e) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    
    const series = document.getElementById('quickSeries').value;
    const model = document.getElementById('quickModel').value;
    if (!series) { alert('请先选择设备系列'); return; }
    if (!model) { alert('请先选择设备型号'); return; }
    
    const formData = new FormData();
    formData.append('series', series);
    formData.append('model', model);
    if (files.length === 1) { formData.append('file', files[0]); }
    else { for (let i = 0; i < files.length; i++) { formData.append('files', files[i]); } }
    
    const originalTreeContent = document.getElementById('leftTree').innerHTML;
    document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⏳</div><div>正在智能分析...</div></div>';
    document.getElementById('quickUploadProgress').style.display = 'block';
    
    try {
        const resp = await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const timerId = setTimeout(() => { xhr.abort(); reject(new Error('请求超时')); }, 300000);
            xhr.upload.onprogress = function(e) {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    const bar = document.getElementById('quickUploadBar');
                    if (bar) bar.style.width = pct + '%';
                }
            };
            xhr.onload = function() {
                clearTimeout(timerId);
                try {
                    const data = JSON.parse(xhr.responseText);
                    resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, json: () => Promise.resolve(data), text: () => Promise.resolve(xhr.responseText) });
                } catch(e) {
                    resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, json: () => Promise.reject(e), text: () => Promise.resolve(xhr.responseText) });
                }
            };
            xhr.onerror = function() { clearTimeout(timerId); reject(new Error('Failed to fetch')); };
            xhr.onabort = function() { clearTimeout(timerId); const e = new Error('请求超时'); e.name = 'TimeoutError'; reject(e); };
            xhr.open('POST', '/api/analyze');
            xhr.send(formData);
        });
        if (!resp.ok) {
            const text = await resp.text();
            try {
                const errorData = JSON.parse(text);
                throw new Error(errorData.error || `服务器错误 ${resp.status}`);
            } catch (parseError) {
                throw new Error(`服务器错误 ${resp.status}: ${text.slice(0, 300)}`);
            }
        }
        const data = await resp.json();
        if (data.error) { alert('上传失败: ' + data.error); document.getElementById('leftTree').innerHTML = originalTreeContent; return; }
        
        if (data.status === 'accepted' && data.analysis_id) {
            document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⏳</div><div id="pollMsg">任务已提交，正在智能分析...</div></div>';
            let pollCount = 0;
            let sseOk = false;
            try {
                const es = new EventSource(`/api/task_events/${data.analysis_id}`);
                sseOk = true;
                const pollEl = document.getElementById('pollMsg');
                es.onmessage = function(e) {
                    pollCount++;
                    try {
                        const d = JSON.parse(e.data);
                        if (d.status === 'completed' && d.redirect_url) { es.close(); window.location.href = d.redirect_url; }
                        else if (d.status === 'failed') { es.close(); alert('分析失败: ' + (d.error || '未知错误')); document.getElementById('leftTree').innerHTML = originalTreeContent; }
                        else if (d.status === 'timeout') { es.close(); alert('分析超时，请重试'); document.getElementById('leftTree').innerHTML = originalTreeContent; }
                        else { if (pollEl) pollEl.textContent = `正在智能分析... (已等待 ${pollCount} 秒)`; }
                    } catch(err) { if (pollEl) pollEl.textContent = `正在智能分析... (已等待 ${pollCount} 秒)`; }
                };
                es.onerror = function() { es.close(); if (pollEl) pollEl.textContent = 'SSE断开，切换轮询...'; setTimeout(function() { quickPollFallback(data.analysis_id, pollCount); }, 1000); };
            } catch(err) { }
            if (!sseOk) { setTimeout(function() { quickPollFallback(data.analysis_id, 0); }, 1000); }
            return;
        }
        
        if (data.redirect_url) { window.location.href = data.redirect_url; }
        else { alert('服务器返回异常，请重试'); document.getElementById('leftTree').innerHTML = originalTreeContent; }
    } catch (err) {
        let message;
        if (err.name === 'TimeoutError') { message = '分析请求超时，请重试'; }
        else if (err.message && err.message.includes('Failed to fetch')) { message = '无法连接到服务器，请确认服务是否已启动'; }
        else { message = `请求失败: ${err.message}`; }
        alert(message);
        document.getElementById('leftTree').innerHTML = originalTreeContent;
    } finally {
        document.getElementById('quickUploadProgress').style.display = 'none';
    }
});

function quickPollFallback(analysisId, startCount) {
    let pollCount = startCount || 0, pollInterval = 1000;
    function poll() {
        pollCount++;
        if (pollCount > 600) { alert('分析超时，请重试'); window.location.reload(); return; }
        fetch(`/api/task_status/${analysisId}`)
            .then(r => r.json())
            .then(d => {
                if (d.status === 'completed' && d.redirect_url) { window.location.href = d.redirect_url; }
                else if (d.status === 'failed') { alert('分析失败: ' + (d.error || '未知错误')); window.location.reload(); }
                else { const pollEl = document.getElementById('pollMsg'); if (pollEl) pollEl.textContent = `正在智能分析... (已等待 ${pollCount} 秒)`; pollInterval = Math.min(pollInterval * 1.5, 10000); setTimeout(poll, pollInterval); }
            })
            .catch(() => { setTimeout(poll, Math.min(pollInterval * 2, 20000)); });
    }
    setTimeout(poll, 1000);
}

function buildDateTree(dateGroups) {
    return dateGroups || [];
}

function renderDateGroupedTree(dateGroups) {
    // renderDateGroupedTree called
    const tree = document.getElementById('leftTree');
    tree.innerHTML = '';
    if (!dateGroups || dateGroups.length === 0) {
        if (_embeddedData && _embeddedData.from_pg) {
            tree.innerHTML = '<div class="empty-state"><div class="icon">⏰</div><div>详细分析数据已过期</div><div style="color:#94a3b8;font-size:0.85rem;margin-top:8px;">摘要信息仍可查看，文件级详情需重新分析</div></div>';
        } else {
            tree.innerHTML = '<div class="empty-state"><div class="icon">📭</div><div>暂无匹配文件</div></div>';
        }
        return;
    }

    dateGroups.forEach(group => {
        const dateDiv = document.createElement('div');
        dateDiv.className = 'date-group';
        let fileNodesHtml = '';
        (group.files || []).forEach(f => {
            const isAspirationFile = f.is_aspiration_file || false;
            const hasAspirationMatch = f.has_aspiration_match || false;
            const hasFault = f.has_fault || false;
            const hasTypes = (f.types || []).length > 0;
            const isReceiveFile = (f.types || []).includes('receive');
            
            let iconClass = 'fas fa-file-alt';
            let iconColor = '#64748b';
            let alertIcon = '';
            
            if (hasFault) {
                iconClass = 'fas fa-bug';
                iconColor = '#dc2626';
            } else if (isAspirationFile && hasAspirationMatch) {
                iconClass = 'fas fa-exclamation-triangle';
                iconColor = '#dc2626';
                alertIcon = '<i class="fas fa-bell" style="color:#dc2626;margin-left:6px;animation:pulse 2s infinite;"></i>';
            } else if (isReceiveFile) {
                iconClass = 'fas fa-download';
                iconColor = '#15803d';
            }
            
            const sizeKB = (f.size / 1024).toFixed(0);
            const tagsHtml = (f.types || []).map(t => {
                const label = t === 'fault' ? '故障' : t === 'sample' ? '样本' : t === 'reagent' ? '试剂' : t === 'receive' ? '接收' : '';
                return `<span class="type-tag type-tag-${t}" onclick="event.stopPropagation(); filterByType('${t}', event)" title="点击筛选${label}文件">${label}</span>`;
            }).join('');
            
            const displayName = f.name.split('/').pop();
            
            fileNodesHtml += `
                <div class="file-node" data-filename="${escapeAttr(f.name)}" data-searchable="${escapeHtml(f.name).toLowerCase()}" title="${escapeHtml(f.name)} (${sizeKB} KB)">
                    <span class="icon"><i class="${iconClass}" style="color:${iconColor};"></i></span>
                    <span class="fname">${escapeHtml(displayName)}${alertIcon}</span>
                    <span class="type-tags">${tagsHtml}</span>
                    <span class="fsize">${sizeKB}K</span>
                </div>
            `;
        });
        dateDiv.innerHTML = `
            <div class="date-header" onclick="toggleDateGroup(this)">
                <span class="arrow"><i class="fas fa-chevron-down"></i></span>
                <span class="date-text"><i class="fas fa-calendar-alt date-icon"></i>${escapeHtml(group.date || '未识别日期')}</span>
                <span class="count">${(group.files || []).length} 个文件</span>
            </div>
            <div class="file-list">${fileNodesHtml}</div>
        `;
        tree.appendChild(dateDiv);
    });

    tree.querySelectorAll('.file-node').forEach(node => {
        node.addEventListener('click', () => {
            selectFile(node.dataset.filename);
        });
    });
}

function toggleDateGroup(header) {
    header.classList.toggle('collapsed');
    const fileList = header.nextElementSibling;
    if (fileList) fileList.classList.toggle('collapsed');
    const arrow = header.querySelector('.arrow i');
    if (arrow) {
        arrow.className = header.classList.contains('collapsed') ? 'fas fa-chevron-right' : 'fas fa-chevron-down';
    }
}

async function loadAnalysisData() {
    let data = _embeddedData;
    if (!data) {
        try {
            const resp = await fetch(`/api/analysis/${ANALYSIS_ID}`);
            if (!resp.ok) {
                document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><div>分析数据加载失败</div></div>';
                return;
            }
            data = await resp.json();
        } catch (err) {
            document.getElementById('leftTree').innerHTML = '<div class="empty-state"><div class="icon">❌</div><div>加载失败: ' + err.message + '</div></div>';
            return;
        }
    }
    try {
    document.getElementById('metaSeries').textContent = '📋 ' + (data.series || '-');
    document.getElementById('metaModel').textContent = '🔧 ' + (data.model || '-');
    
    if (data.analysis_type === 'reagent_cooling') {
        document.querySelector('.topbar h2').innerHTML = '<i class="fas fa-snowflake"></i> 试剂制冷排查报告';
    }
    
    if (data.series) {
        document.getElementById('quickSeries').value = data.series;
        document.getElementById('quickSeries').dispatchEvent(new Event('change'));
    }
    if (data.model) {
        setTimeout(() => { document.getElementById('quickModel').value = data.model; }, 300);
    }
    
    const uploadedInfoEl = document.getElementById('uploadedInfo');
    if (uploadedInfoEl) {
        uploadedInfoEl.innerHTML = `文件: <strong>${escapeHtml(data.file_name || '-')}</strong><br>分析时间: ${escapeHtml(data.analyzed_at || '-')}`;
    }
    document.getElementById('metaFile').textContent = '';
    document.getElementById('metaTime').textContent = '';

    allDateGroups = data.date_groups || [];
    hasMoreFiles = data.has_more_files || false;
    zipTotalCandidates = data.zip_total_candidates || 0;
    zipProcessed = data.zip_processed || 0;

    const s = data.summary || {};
    document.getElementById('summaryRow').innerHTML = `
        <span class="tag tag-fault" onclick="filterByType('fault', event)" title="点击筛选故障文件"><i class="fas fa-bug"></i> 故障 ${s.fault||0}</span>
        <span class="tag tag-sample" onclick="filterByType('sample', event)" title="点击筛选样本空吸文件"><i class="fas fa-vial"></i> 样本 ${s.sample||0}</span>
        <span class="tag tag-reagent" onclick="filterByType('reagent', event)" title="点击筛选试剂空吸文件"><i class="fas fa-flask"></i> 试剂 ${s.reagent||0}</span>
        <span class="tag tag-receive" onclick="filterByType('receive', event)" title="点击筛选接收数据文件"><i class="fas fa-download"></i> 接收 ${s.receive||0}</span>
        <span class="tag tag-all" onclick="filterByType('all', event)" title="显示全部文件"><i class="fas fa-list"></i> 全部</span>
    `;
    
    let displayedFileCount = 0;
    allDateGroups.forEach(group => {
        displayedFileCount += (group.files || []).length;
    });

    renderDateGroupedTree(allDateGroups);
    updateFooter();

    if (zipTotalCandidates > 0) {
        document.getElementById('metaFile').textContent = ' (已加载 ' + displayedFileCount + '/' + zipTotalCandidates + ' 文件)';
    }
    } catch (err) {
        console.error('加载分析数据失败:', err.message);
        document.getElementById('leftTree').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>加载失败: ${err.message}</div></div>`;
    }
}

function updateFooter() {
    const footer = document.getElementById('leftFooter');
    let html = '';
    if (hasMoreFiles) {
        // 计算当前显示的文件数量
        let displayedFileCount = 0;
        allDateGroups.forEach(group => {
            displayedFileCount += (group.files || []).length;
        });
        
        html += `<button class="btn-load-more" id="btnLoadMoreFiles" onclick="loadMoreFiles()" style="background:#e8f5e9;border-color:#4caf50;color:#2e7d32;margin-top:4px;">
            📦 加载更多文件 (已加载 ${displayedFileCount}/${zipTotalCandidates})
        </button>`;
    }
    footer.innerHTML = html;
}

async function loadMoreFiles() {
    if (isLoadingMoreFiles) return;
    isLoadingMoreFiles = true;
    const btn = document.getElementById('btnLoadMoreFiles');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 加载中...'; }
    try {
        const resp = await fetch(`/api/analysis/${ANALYSIS_ID}/load-more?csrf_token=${encodeURIComponent(CSRF_TOKEN)}`, { method: 'POST' });
        const data = await resp.json();
        if (!data.success) {
            alert('加载失败: ' + (data.error || '未知错误'));
            return;
        }
        allDateGroups = data.date_groups || [];
        hasMoreFiles = data.has_more_files || false;
        zipProcessed = data.zip_processed || 0;
        zipTotalCandidates = data.zip_total_candidates || 0;

        const s = data.summary || {};
        document.getElementById('summaryRow').innerHTML = `
            <span class="tag tag-fault" onclick="filterByType('fault', event)" title="点击筛选故障文件"><i class="fas fa-bug"></i> 故障 ${s.fault||0}</span>
            <span class="tag tag-sample" onclick="filterByType('sample', event)" title="点击筛选样本空吸文件"><i class="fas fa-vial"></i> 样本 ${s.sample||0}</span>
            <span class="tag tag-reagent" onclick="filterByType('reagent', event)" title="点击筛选试剂空吸文件"><i class="fas fa-flask"></i> 试剂 ${s.reagent||0}</span>
            <span class="tag tag-receive" onclick="filterByType('receive', event)" title="点击筛选接收数据文件"><i class="fas fa-download"></i> 接收 ${s.receive||0}</span>
            <span class="tag tag-all" onclick="filterByType('all', event)" title="显示全部文件"><i class="fas fa-list"></i> 全部</span>
        `;

        renderDateGroupedTree(allDateGroups);
        updateFooter();
        
        // 计算当前显示的文件数量
        let displayedFileCount = 0;
        allDateGroups.forEach(group => {
            displayedFileCount += (group.files || []).length;
        });
        document.getElementById('metaFile').textContent = ' (已加载 ' + displayedFileCount + '/' + zipTotalCandidates + ' 文件)';
    } catch (err) {
        console.error('加载更多文件失败:', err.message);
        alert('加载更多失败: ' + err.message);
    } finally {
        isLoadingMoreFiles = false;
        if (btn) { btn.disabled = false; btn.textContent = '📦 加载更多文件'; }
    }
}

// 文件类型筛选
let currentFilterType = 'all';

function filterByType(type, evt) {
    evt = evt || window.event;
    // filterByType called
    
    // 防止重复点击
    if (currentFilterType === type && type !== 'all') {
        // 双击取消筛选，显示全部
        type = 'all';
    }
    
    currentFilterType = type;
    
    // 更新标签激活状态
    document.querySelectorAll('.tag').forEach(tag => {
        tag.classList.remove('active');
        // 添加点击动画
        tag.style.transform = 'scale(0.95)';
        setTimeout(() => {
            tag.style.transform = '';
        }, 150);
    });
    
    // 激活当前标签
    const currentTag = evt.target.closest('.tag');
    if (currentTag) {
        currentTag.classList.add('active');
        currentTag.style.transform = 'scale(1.15)';
        setTimeout(() => {
            currentTag.style.transform = '';
        }, 200);
    }
    
    // 筛选文件
    const filteredGroups = [];
    allDateGroups.forEach(group => {
        const filteredFiles = (group.files || []).filter(f => {
            if (type === 'all') return true;
            const types = f.types || [];
            return types.includes(type);
        });
        
        if (filteredFiles.length > 0) {
            filteredGroups.push({
                date: group.date,
                files: filteredFiles
            });
        }
    });
    
    // 添加淡出淡入动画
    const tree = document.getElementById('leftTree');
    tree.style.opacity = '0';
    tree.style.transform = 'translateY(-10px)';
    
    setTimeout(() => {
        // 渲染筛选后的文件树
        renderDateGroupedTree(filteredGroups);
        
        // 淡入动画
        tree.style.transition = 'all 0.3s ease';
        tree.style.opacity = '1';
        tree.style.transform = 'translateY(0)';
    }, 150);
}

function selectFile(filename) {
    currentFileName = filename;
    const showFault = filename && filename.includes('接收数据记录');
    document.querySelectorAll('.fault-btn').forEach(b => b.style.display = showFault ? '' : 'none');
    const prevActive = document.querySelector('.file-node.active');
    if (prevActive) prevActive.classList.remove('active');
    const targetNode = document.querySelector(`.file-node[data-filename="${CSS.escape(filename)}"]`);
    if (targetNode) {
        targetNode.classList.add('active');
        targetNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    document.getElementById('rightPlaceholder').style.display = 'none';
    document.getElementById('rightContent').style.display = 'flex';
    document.getElementById('fileTitle').textContent = '📄 ' + filename;
    const cached = fileCacheGet(filename);
    if (cached) {
        document.getElementById('fileMeta').textContent = cached._meta;
        renderFileContent(cached);
        return;
    }
    document.getElementById('fileMeta').textContent = '加载中...';
    document.getElementById('rightBody').innerHTML = '<div style="text-align:center;padding:40px;color:#999;font-size:0.85rem;">⏳ 加载文件内容...</div>';
    loadFileContent(filename);
}

 async function loadFileContent(filename) {
     try {
         const resp = await fetch(`/api/analysis/${ANALYSIS_ID}/file?name=${encodeURIComponent(filename)}`);
         if (!resp.ok) {
             let errMsg = '文件加载失败';
             try { const ed = await resp.json(); if (ed.error) errMsg = ed.error; } catch(e) {}
             if (resp.status === 410 || errMsg.includes('已清理') || errMsg.includes('过期')) {
                 document.getElementById('rightBody').innerHTML = `<div class="empty-state"><div class="icon">⏰</div><div>分析结果已过期</div><div style="font-size:0.85rem;color:#94a3b8;margin-top:8px;">缓存已清理，请重新上传文件进行分析</div></div>`;
             } else {
                 document.getElementById('rightBody').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>${errMsg}</div></div>`;
             }
             return;
         }
        const data = await resp.json();
        data._meta = `${(data.size / 1024).toFixed(0)} KB | ${data.analysis.length} 条匹配`;
        fileCacheSet(filename, data);
        document.getElementById('fileMeta').textContent = data._meta;
        renderFileContent(data);
        _updateFileNodeIcon(filename, data);
    } catch (err) {
        document.getElementById('rightBody').innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>加载失败: ${err.message}</div></div>`;
    }
}
function _updateFileNodeIcon(filename, data) {
    const node = document.querySelector(`.file-node[data-filename="${CSS.escape(filename)}"]`);
    if (!node) return;
    const iconSpan = node.querySelector('.icon i');
    if (!iconSpan) return;
    const types = Array.from(node.querySelectorAll('.type-tag')).map(t => t.classList.contains('type-tag-sample') ? 'sample' : t.classList.contains('type-tag-reagent') ? 'reagent' : t.classList.contains('type-tag-fault') ? 'fault' : t.classList.contains('type-tag-receive') ? 'receive' : '');
    const isAspirationFile = types.includes('sample') || types.includes('reagent');
    const hasAspirationMatch = data.analysis && data.analysis.some(item => item.type === 'keyword_match' && (item.keywords || []).some(kw => kw && kw.includes('空吸')));
    const hasFault = data.has_fault || false;
    const isReceiveFile = types.includes('receive');
    let alertBell = node.querySelector('.fname .fa-bell');
    if (hasFault) {
        iconSpan.className = 'fas fa-bug';
        iconSpan.style.color = '#dc2626';
        if (alertBell) alertBell.remove();
    } else if (isAspirationFile && hasAspirationMatch) {
        iconSpan.className = 'fas fa-exclamation-triangle';
        iconSpan.style.color = '#dc2626';
        const fnameSpan = node.querySelector('.fname');
        if (fnameSpan && !alertBell) {
            fnameSpan.insertAdjacentHTML('beforeend', '<i class="fas fa-bell" style="color:#dc2626;margin-left:6px;animation:pulse 2s infinite;"></i>');
        }
    } else if (isReceiveFile) {
        iconSpan.className = 'fas fa-download';
        iconSpan.style.color = '#15803d';
        if (alertBell) alertBell.remove();
    } else {
        iconSpan.className = 'fas fa-file-alt';
        iconSpan.style.color = '#64748b';
        if (alertBell) alertBell.remove();
    }
}
function renderFileContent(data) {
    const body = document.getElementById('rightBody');
    const analysis = data.analysis || [];
    if (!Array.isArray(analysis)) {
        body.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><div>分析数据格式异常</div></div>';
        return;
    }

    try {
        // ===== 优先使用后端生成的 html_content =====
        if (data.html_content) {
            // 样本/试剂分组（仅当没有故障时才显示）
            const hasFault = data.has_fault || false;
            let sampleMatches = [];
            let reagentMatches = [];
            let hasMatches = false;
            
            if (!hasFault) {
                sampleMatches = analysis.filter(item => 
                    item.type === 'keyword_match' && 
                    Array.isArray(item.keywords) && 
                    item.keywords.some(kw => ['样本空吸', '样本不足'].includes(kw))
                );
                reagentMatches = analysis.filter(item => 
                    item.type === 'keyword_match' && 
                    Array.isArray(item.keywords) && 
                    item.keywords.some(kw => ['试剂空吸', '试剂不足'].includes(kw))
                );
                hasMatches = sampleMatches.length > 0 || reagentMatches.length > 0;
            }

            function renderGroup(title, icon, items, emptyMsg, borderColor, bgColor, groupId) {
                if (!items || items.length === 0) return '';
                let groupHtml = `<div class="db-analysis-section" style="margin-top:12px;">`;
                groupHtml += `<div class="db-separator" style="display:flex;align-items:center;justify-content:space-between;">
                    <span><i class="${icon}" style="margin-right:6px;"></i>${title} (${items.length} 条)</span>
                    <button class="btn-view-full" onclick="openMatchModal('${groupId}', '${title}')"><i class="fas fa-expand"></i>查看全文</button>
                </div>`;
                groupHtml += `<div id="${groupId}" style="display:none;"></div>`;
                let previewHtml = '';
                items.slice(0, 10).forEach((item, idx) => {
                    previewHtml += `<div style="background:linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);border-left:3px solid var(--warning);padding:6px 10px;margin:2px 0;border-radius:6px;font-family:monospace;font-size:0.92rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;box-shadow:var(--shadow-sm);"><i class="fas fa-file-alt" style="color:var(--warning);margin-right:6px;"></i>${escapeHtml(item.original_text || '')}</div>`;
                    previewHtml += `<div style="color:var(--success);padding:4px 10px;font-size:0.92rem;line-height:1.4;background:rgba(16,185,129,0.05);border-radius:4px;margin:2px 0;"><i class="fas fa-lightbulb" style="margin-right:6px;"></i><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
                    if (idx < Math.min(items.length, 10) - 1) {
                        previewHtml += `<div style="height:8px;"></div>`;
                    }
                });
                groupHtml += `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:8px;padding:10px;overflow-y:auto;box-shadow:var(--shadow);">${previewHtml}</div>`;
                if (items.length > 10) {
                    groupHtml += `<div style="text-align:center;color:#64748b;font-size:0.85rem;padding:4px;">... 还有 ${items.length - 10} 条匹配，点击"查看全文"查看全部</div>`;
                }
                groupHtml += `</div>`;
                groupHtml += `</div>`;
                return groupHtml;
            }
            
            window.matchData = {};
            window.matchData['sampleMatches'] = sampleMatches;
            window.matchData['reagentMatches'] = reagentMatches;

            let html = '';
            
            // 如果有匹配信息，分为75%和25%
            if (hasMatches) {
                html = `<div class="content-section" style="height:75%;display:flex;flex-direction:column;">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="flex:1;white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:1rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;overflow-y:auto;overflow-x:hidden;">
                        ${data.html_content}
                    </div>
                </div>
                <div class="content-section" style="height:25%;display:flex;flex-direction:column;margin-top:8px;">
                    <div class="section-label"><i class="fas fa-search" style="margin-right:6px;"></i>匹配信息</div>
                    <div style="flex:1;overflow-y:auto;">
                        ${renderGroup('样本空吸匹配', 'fas fa-vial', sampleMatches, '暂无样本空吸匹配', 'var(--info)', '#e0f2fe', 'sampleMatches')}
                        ${renderGroup('试剂空吸匹配', 'fas fa-flask', reagentMatches, '暂无试剂空吸匹配', 'var(--purple)', '#f3e8ff', 'reagentMatches')}
                    </div>
                </div>`;
            } else if (hasFault) {
                html = `<div class="content-section">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:1rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
                        ${data.html_content}
                    </div>
                </div>
                <div style="margin-top:12px;padding:10px 14px;background:linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);border-radius:8px;color:#92400e;font-size:0.85rem;border-left:4px solid var(--warning);box-shadow:var(--shadow-sm);"><i class="fas fa-exclamation-triangle" style="margin-right:8px;"></i>检测到故障匹配，仅显示故障诊断信息</div>`;
            } else {
                // 没有匹配信息，正常显示
                html = `<div class="content-section">
                    <div class="section-label"><i class="fas fa-file-medical-alt" style="margin-right:6px;"></i>原始文档与故障对比</div>
                    <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:1rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
                        ${data.html_content}
                    </div>
                </div>`;
            }

            _fullFileContent = data.content || '';
            body.innerHTML = html;
            const truncMarker = document.getElementById('htmlTruncationMarker');
            if (truncMarker && data.content) {
                const totalLines = parseInt(truncMarker.dataset.total) || 0;
                const renderedLines = parseInt(truncMarker.dataset.rendered) || 5000;
                const allLines = data.content.split('\n');
                _pendingLines = allLines.slice(renderedLines);
                _renderedLineCount = renderedLines;
                _totalLineCount = totalLines;
                _pendingAdviceMap = {};
                if (data.analysis) {
                    data.analysis.forEach(item => {
                        if (item.type === 'motor_status_match' && item.original_text && item.advice) {
                            const orig = item.original_text.trim();
                            if (orig && !_pendingAdviceMap[orig]) _pendingAdviceMap[orig] = item.advice;
                        }
                    });
                }
                truncMarker.onclick = loadMoreLines;
            }
            return;  // 使用后端生成的内容，直接结束
        }

        // ===== 兼容旧逻辑（如果后端没有 html_content，则走原流程） =====
        // 以下为原有的渲染逻辑（您恢复后的版本），保留作为后备
        const content = data.content || '';
        const adviceMap = {};
        analysis.forEach(item => {
            if (item.type === 'motor_status_match') {
                const orig = item.original_text ? item.original_text.trim() : '';
                if (orig && item.advice) {
                    if (!adviceMap[orig]) {
                        adviceMap[orig] = item.advice;
                    }
                }
            }
        });

        const lines = content.split('\n');
        const MAX_RENDER_LINES = 5000;
        const totalLines = lines.length;
        const renderLines = lines.slice(0, MAX_RENDER_LINES);
        let html = `
            <div class="content-section">
                <div class="section-label">📝 原始文档内容${totalLines > MAX_RENDER_LINES ? ` <span style="color:#94a3b8;font-size:0.8em;">(显示前${MAX_RENDER_LINES}行/共${totalLines}行)</span>` : ''}</div>
                <div class="raw-content" style="white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:1rem; line-height:1.6; background:#fafbfc; padding:12px; border-radius:6px; border:1px solid #e8ecf1;">
        `;
        _fullFileContent = data.content || '';
        _pendingLines = lines.slice(MAX_RENDER_LINES);
        _pendingAdviceMap = adviceMap;
        _renderedLineCount = Math.min(lines.length, MAX_RENDER_LINES);
        _totalLineCount = lines.length;
        renderLines.forEach(line => {
            const trimmed = line.trim();
            html += escapeHtml(line) + '\n';
            if (trimmed && adviceMap[trimmed]) {
                html += '<span style="color:#155724; font-weight:600; display:block; margin-left:1.2em;">诊断建议：</span>';
                html += '<span style="display:block; margin-left:2.4em; color:#2d6a4f;">' + escapeHtml(adviceMap[trimmed]) + '</span>\n';
            }
        });
        if (totalLines > MAX_RENDER_LINES) {
            html += `<div id="loadMoreHint" style="text-align:center;padding:12px;color:#6366f1;font-size:0.85rem;border-top:1px dashed #c7d2fe;margin-top:8px;cursor:pointer;" onclick="loadMoreLines()"><i class="fas fa-angle-double-down" style="margin-right:6px;"></i>加载更多行 <span id="loadMoreInfo">(已渲染 ${MAX_RENDER_LINES} / 共${totalLines} 行)</span></div>`;
        }
        html += `</div></div>`;

        const sampleMatches = analysis.filter(item => 
            item.type === 'keyword_match' && 
            Array.isArray(item.keywords) && 
            item.keywords.some(kw => ['样本空吸', '样本不足'].includes(kw))
        );
        const reagentMatches = analysis.filter(item => 
            item.type === 'keyword_match' && 
            Array.isArray(item.keywords) && 
            item.keywords.some(kw => ['试剂空吸', '试剂不足'].includes(kw))
        );

        function renderGroup(title, icon, items, emptyMsg, borderColor, bgColor, groupId) {
            if (!items || items.length === 0) return '';
            let groupHtml = `<div class="db-analysis-section" style="margin-top:16px;">`;
            groupHtml += `<div class="db-separator" style="display:flex;align-items:center;justify-content:space-between;">
                <span>${icon} ${title} (${items.length} 条)</span>
                <button class="btn-view-full" onclick="openMatchModal('${groupId}', '${title}')"><i class="fas fa-expand"></i> 查看全文</button>
            </div>`;
            groupHtml += `<div id="${groupId}" style="display:none;">`;
            items.forEach((item, idx) => {
                groupHtml += `<div style="padding:8px;margin:6px 0;background:${bgColor};border-left:3px solid ${borderColor};border-radius:6px;">
                    <div style="font-family:monospace;font-size:0.92rem;white-space:pre-wrap;word-break:break-word;background:#fefce8;border:1px solid #fde047;padding:6px 10px;border-radius:4px;margin-bottom:4px;">${escapeHtml(item.original_text || '')}</div>
                    <div style="font-size:0.92rem;color:#166534;"><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>
                </div>`;
            });
            groupHtml += `</div>`;
            let previewHtml = '';
            items.slice(0, 10).forEach((item, idx) => {
                previewHtml += `<div style="background:#fefce8;border-left:3px solid #fde047;padding:4px 8px;margin:2px 0;border-radius:4px;font-family:monospace;font-size:0.92rem;line-height:1.3;white-space:pre-wrap;word-break:break-word;">${escapeHtml(item.original_text || '')}</div>`;
                previewHtml += `<div style="color:#166534;padding:2px 8px;font-size:0.92rem;line-height:1.3;"><strong>💡 诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
                if (idx < Math.min(items.length, 10) - 1) {
                    previewHtml += `<div style="height:6px;"></div>`;
                }
            });
            groupHtml += `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:6px;padding:8px;max-height:300px;overflow-y:auto;">${previewHtml}</div>`;
            if (items.length > 10) {
                groupHtml += `<div style="text-align:center;color:#64748b;font-size:0.85rem;padding:4px;">... 还有 ${items.length - 10} 条匹配，点击"查看全文"查看全部</div>`;
            }
            groupHtml += `</div>`;
            groupHtml += `</div>`;
            return groupHtml;
        }
        
        window.matchData = {};
        window.matchData['sampleMatches'] = sampleMatches;
        window.matchData['reagentMatches'] = reagentMatches;

        html += renderGroup('样本空吸匹配', '🧪', sampleMatches, '暂无样本空吸匹配', '#17a2b8', '#e3f2fd', 'sampleMatches');
        html += renderGroup('试剂空吸匹配', '🧫', reagentMatches, '暂无试剂空吸匹配', '#6f42c1', '#f3e5f5', 'reagentMatches');

        body.innerHTML = html;
    } catch (err) {
        console.error('渲染文件内容失败:', err.message);
        body.innerHTML = `<div class="empty-state"><div class="icon">❌</div><div>渲染失败: ${escapeHtml(err.message)}</div></div>`;
    }
}

function openFullModal() {
    if (!currentFileName) return;
    const modal = document.getElementById('fullModal');
    document.getElementById('modalTitle').textContent = '📄 ' + currentFileName;
        const src = document.getElementById('rightBody');
        if (src.innerHTML.length > 5000000) { document.getElementById('modalBody').innerHTML = '<div style="padding:20px;color:#94a3b8;">文件过大，请使用搜索功能查看</div>'; }
        else { document.getElementById('modalBody').innerHTML = src.innerHTML; }
    modal.style.display = 'flex';
}

function closeFullModal() {
    document.getElementById('fullModal').style.display = 'none';
}

let _pendingLines = [];
let _pendingAdviceMap = {};
let _fullFileContent = '';
let _renderedLineCount = 0;
let _totalLineCount = 0;
const _LOAD_MORE_BATCH = 8000;

function loadMoreLines() {
    const rawEl = document.querySelector('.raw-content');
    if (!rawEl || _pendingLines.length === 0) return;
    let hintEl = document.getElementById('loadMoreHint') || document.getElementById('htmlTruncationMarker');
    if (!hintEl) return;
    if (hintEl.id === 'htmlTruncationMarker') hintEl.id = 'loadMoreHint';
    hintEl.innerHTML = '<i class="fas fa-spinner fa-spin" style="margin-right:6px;"></i>加载中...';
    setTimeout(() => {
        const batch = _pendingLines.splice(0, _LOAD_MORE_BATCH);
        const fragParts = [];
        batch.forEach(line => {
            const trimmed = line.trim();
            fragParts.push(escapeHtml(line), '\n');
            if (trimmed && _pendingAdviceMap[trimmed]) {
                fragParts.push('<span style="color:#155724; font-weight:600; display:block; margin-left:1.2em;">诊断建议：</span>');
                fragParts.push('<span style="display:block; margin-left:2.4em; color:#2d6a4f;">', escapeHtml(_pendingAdviceMap[trimmed]), '</span>\n');
            }
        });
        _renderedLineCount += batch.length;
        hintEl = document.getElementById('loadMoreHint');
        if (hintEl) {
            if (_pendingLines.length > 0) {
                hintEl.innerHTML = '<i class="fas fa-angle-double-down" style="margin-right:6px;"></i>加载更多行 <span id="loadMoreInfo">(已渲染 ' + _renderedLineCount + ' / 共' + _totalLineCount + ' 行)</span>';
                hintEl.onclick = loadMoreLines;
            } else {
                hintEl.innerHTML = '<i class="fas fa-check-circle" style="margin-right:6px;color:#22c55e;"></i>全部加载完成 (共' + _totalLineCount + ' 行)';
                hintEl.onclick = null;
                hintEl.style.cursor = 'default';
            }
        }
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = fragParts.join('');
        while (tempDiv.firstChild) {
            rawEl.insertBefore(tempDiv.firstChild, hintEl);
        }
    }, 50);
}

const FAULT_KEYWORDS = {
    cooling: { title: '制冷故障', icon: 'fa-snowflake', color: '#0ea5e9', keywords: ['制冷', '温度', '冷凝', '压缩机', '散热', '恒温', 'cooling', 'temperature', 'temp', 'TEC', 'peltier'] },
    sample: { title: '样本空吸故障', icon: 'fa-vial', color: '#f59e0b', keywords: ['样本空吸', '样本不足', '吸样', 'sample', '空吸', 'sample aspirate', '样本吸'] },
    reagent: { title: '试剂空吸故障', icon: 'fa-flask', color: '#8b5cf6', keywords: ['试剂空吸', '试剂不足', '吸试剂', 'reagent', '试剂吸', 'reagent aspirate', '试剂空'] },
    gripper: { title: '抓手运行故障', icon: 'fa-hand-paper', color: '#ef4444', keywords: ['抓手', '机械手', '抓取', 'gripper', 'arm', '手抓', 'grip', '抓针', '抓手故障', '抓手异常'] }
};

function hexToTemp(hexStr) {
    const val = parseInt(hexStr, 16);
    if (isNaN(val)) return '--';
    return (val / 10).toFixed(1) + '°C';
}

function parseCoolingFlags(flagHex) {
    const val = parseInt(flagHex, 16);
    if (isNaN(val)) return { raw: flagHex, val: 0, flagItems: [] };
    const bitDefs = [
        { bit: 0x01, on: '制冷开启', off: '制冷关闭' },
        { bit: 0x02, on: '内部制冷风扇开启', off: '内部制冷风机关闭' },
        { bit: 0x04, on: '水冷泵开启', off: '水冷泵关闭' },
        { bit: 0x08, on: '制冷泵开关开启', off: '制冷泵开关关闭' }
    ];
    const flagItems = [];
    for (const d of bitDefs) {
        const isOn = (val & d.bit) !== 0;
        flagItems.push({ text: isOn ? d.on : d.off, on: isOn });
    }
    return { raw: flagHex, val, flagItems };
}

function parseCoolingLine(line, series) {
    const match = line.match(/<(\d+-\d+)>\s+0C\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})/);
    if (!match) return null;
    const addr = match[1];
    const ok = match[2] === '80';
    const status = ok ? '查询成功' : '查询失败';
    const reagentTemp = ok ? hexToTemp(match[3] + match[4]) : '--';
    const sensorTemp = ok ? hexToTemp(match[5] + match[6]) : '--';
    const flagInfo = ok ? parseCoolingFlags(match[7]) : { raw: match[7], val: parseInt(match[7],16), flagItems: [] };
    const pwm = parseInt(match[8], 16);
    const pwmPct = ok ? (isNaN(pwm) ? '--' : pwm + '%') : '--';
    return { cmd: '0C', addr, status, reagentTemp, sensorTemp, flagInfo, pwm: pwmPct, series: 'SMART6500', raw: match[0], dataValid: ok };
}

function parseCoolingLine500_0C(line) {
    const match = line.match(/<(\d+-\d+)>\s+0C\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})/);
    if (!match) return null;
    const addr = match[1];
    const ok = match[2] === '80';
    const status = ok ? '查询成功' : '查询失败';
    const reagentTemp = ok ? hexToTemp(match[3] + match[4]) : '--';
    const coldEndTemp = ok ? hexToTemp(match[5] + match[6]) : '--';
    const hotEndTemp = ok ? hexToTemp(match[7] + match[8]) : '--';
    const overTemp = [];
    if (ok) {
        const rVal = parseFloat(reagentTemp);
        const cVal = parseFloat(coldEndTemp);
        const hVal = parseFloat(hotEndTemp);
        if (!isNaN(cVal) && cVal > 35) overTemp.push('冷端' + coldEndTemp + '>35°C');
        if (!isNaN(hVal) && hVal > 60) overTemp.push('热端' + hotEndTemp + '>60°C');
        if (!isNaN(rVal) && rVal > 40) overTemp.push('显示' + reagentTemp + '>40°C');
    }
    return { cmd: '0C', addr, status, reagentTemp, coldEndTemp, hotEndTemp, overTemp, series: 'SMART500', raw: match[0], dataValid: ok };
}

function getCoolingStateText(stateHex) {
    const val = parseInt(stateHex, 16);
    if (isNaN(val)) return { text: '未知', normal: false };
    switch(val) {
        case 0: return { text: '制冷未开启', normal: false };
        case 1: return { text: '制冷开启', normal: true };
        case 2: return { text: '制冷温度超限', normal: false };
        case 3: return { text: '制冷温度超限', normal: false };
        case 5: return { text: '除冰模式', normal: true };
        default: return { text: '未知状态(' + val + ')', normal: false };
    }
}

function parseCoolingLine500_1C(line) {
    const match = line.match(/<(\d+-\d+)>\s+1C\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})/);
    if (!match) return null;
    const addr = match[1];
    const status = match[2] === '80' ? '查询成功' : '查询失败';
    const pwm = parseInt(match[3], 16);
    const pwmPct = isNaN(pwm) ? '--' : pwm + '%';
    const coolState = getCoolingStateText(match[4]);
    const tecVolt = (parseInt(match[5], 16) / 10).toFixed(1) + 'V';
    const tecCurr = (parseInt(match[6], 16) / 10).toFixed(1) + 'A';
    const pumpSpeed = parseInt(match[7], 16);
    const pumpStr = isNaN(pumpSpeed) ? '--' : pumpSpeed + '转/秒';
    return { cmd: '1C', addr, status, pwm: pwmPct, coolState, tecVolt, tecCurr, pumpSpeed: pumpStr, series: 'SMART500', raw: match[0] };
}


// ========== 加注针监测数据解析 ==========
function hexToSigned16(hexStr) {
    const val = parseInt(hexStr, 16);
    if (isNaN(val)) return 0;
    return val > 0x7FFF ? val - 0x10000 : val;
}

function parseNeedleLine(line, series) {
    // Match: <addr> BX/CX/DX/EX 80 XX XX XX XX XX
    const match = line.match(/<([0-9A-Fa-f]+-[0-9A-Fa-f]+)>\s+([BCDE])([0-3])\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})/);
    if (!match) return null;
    const addr = match[1];
    const cmdType = match[2]; // B/C/D/E
    const cmdNum = match[3]; // 0/1/2/3
    const cmd = cmdType + cmdNum;
    const ok = match[4] === '80';
    const status = ok ? '查询成功' : '查询失败';
    const b1 = match[5] + match[6]; // 2-byte value 1
    const b2 = match[7] + match[8]; // 2-byte value 2
    const b3 = match[9] + match[10]; // 2-byte value 3
    const v1 = parseInt(b1, 16);
    const v2 = parseInt(b2, 16);
    const v3 = hexToSigned16(b3); // signed for E command difference

    // Determine needle type
    let needleType = '未知';
    if (series === 'SMART6500') {
        if (addr === '00-98') needleType = '样本针';
        else if (addr === '00-A0' || addr === '00-a0') {
            needleType = cmdNum === '1' ? '试剂针1' : cmdNum === '2' ? '试剂针2' : cmdNum === '3' ? '试剂针3' : '试剂针';
        }
    } else {
        if (addr === '00-98') {
            needleType = cmdNum === '0' ? '样本针' : '试剂针';
        }
    }

    let fields = {};
    if (!ok) {
        fields = { v1: '--', v2: '--', v3: '--', desc: '查询失败，数据无效' };
    } else if (cmdType === 'B') {
        fields = {
            label1: '第1次接触位置', val1: v1 + ' (0x' + b1 + ')',
            label2: '确认液位位置', val2: v2 + ' (0x' + b2 + ')',
            label3: '差值', val3: v3 + ' (0x' + b3 + ')',
            desc: '探液高度值监测'
        };
    } else if (cmdType === 'C') {
        fields = {
            label1: '吸液前液位', val1: v1 + ' (0x' + b1 + ')',
            label2: '吸液后液位', val2: v2 + ' (0x' + b2 + ')',
            label3: '脱离后液位', val3: v3 + ' (0x' + b3 + ')',
            desc: '脱离液面采样值'
        };
    } else if (cmdType === 'D') {
        fields = {
            label1: '采样总次数', val1: v1 + ' (0x' + b1 + ')',
            label2: 'SORT过滤次数', val2: v2 + ' (0x' + b2 + ')',
            label3: '静态过滤次数', val3: v3 + ' (0x' + b3 + ')',
            desc: '过滤算法采样点数'
        };
    } else if (cmdType === 'E') {
        fields = {
            label1: '下行行程', val1: v1 + ' (0x' + b1 + ')',
            label2: '上复位行程', val2: v2 + ' (0x' + b2 + ')',
            label3: '行程差值', val3: v3 + ' (0x' + b3 + ')',
            desc: '加注针探液行程监测'
        };
    }

    return { cmd, cmdType, addr, status, ok, needleType, fields, series, raw: match[0] };
}

function analyzeNeedleAbnormal(parsed, series) {
    if (!parsed || !parsed.ok) return { abnormal: false, reason: '' };

    if (parsed.cmdType === 'B') {
        var diff = parseInt(parsed.fields.val3);
        if (!isNaN(diff)) {
            if (diff > 60) return { abnormal: true, level: '严重', reason: '探液高度差值' + diff + ' > 60，可能存在气泡或干扰导致没有连续探液成功' };
            if (diff > 40) return { abnormal: true, level: '警告', reason: '探液高度差值' + diff + ' > 40，可能空吸' };
        }
    }

    if (parsed.cmdType === 'C') {
        var afterLevel = parseInt(parsed.fields.val3);
        if (!isNaN(afterLevel) && afterLevel === 0) {
            return { abnormal: true, level: '严重', reason: '脱离液面后液位为0，脱离液面失败(空吸)' };
        }
    }

    if (parsed.cmdType === 'D') {
        var sortCount = parseInt(parsed.fields.val2);
        var staticCount = parseInt(parsed.fields.val3);
        if (!isNaN(sortCount) && sortCount > 10) {
            return { abnormal: true, level: '警告', reason: 'SORT过滤次数' + sortCount + ' > 10，影响液位探测' };
        }
        if (!isNaN(staticCount) && staticCount > 10) {
            return { abnormal: true, level: '警告', reason: '静态过滤次数' + staticCount + ' > 10，影响液位探测' };
        }
    }

    if (parsed.cmdType === 'E') {
        var diff = parseInt(parsed.fields.val3);
        if (!isNaN(diff)) {
            var absDiff = Math.abs(diff);
            if (absDiff > 20) return { abnormal: true, level: '严重', reason: '行程差值' + diff + '，绝对值>20，针上下行程异常' };
        }
    }

    return { abnormal: false, reason: '' };
}

function groupNeedleData(lines, needleCategory, series) {
    const isSmart6500 = series === 'SMART6500';
    const results = [];

    let returnRegex = null;
    if (needleCategory === 'sample') {
        returnRegex = /<00-98>\s+0C\s+/i;
    } else if (needleCategory === 'reagent') {
        if (isSmart6500) {
            returnRegex = /<00-A0>\s+(0D|14|1B)\s+/i;
        } else {
            returnRegex = /<00-98>\s+0D\s+/i;
        }
    }

    lines.forEach((line, idx) => {
        const parsed = parseNeedleLine(line, series);
        if (parsed) {
            let isTarget = false;
            if (needleCategory === 'sample') {
                if (isSmart6500) {
                    isTarget = parsed.addr === '00-98' && parsed.cmd[1] === '1';
                } else {
                    isTarget = parsed.addr === '00-98' && parsed.cmd[1] === '0';
                }
            } else if (needleCategory === 'reagent') {
                if (isSmart6500) {
                    isTarget = (parsed.addr === '00-A0' || parsed.addr === '00-a0') && parsed.cmd[1] !== '0';
                } else {
                    isTarget = parsed.addr === '00-98' && parsed.cmd[1] === '1';
                }
            }
            if (isTarget) {
                const abnormal = analyzeNeedleAbnormal(parsed, series);
                results.push({ lineNum: idx + 1, text: line, parsed: parsed, abnormal: abnormal, isReturn: false });
                return;
            }
        }
        if (returnRegex && returnRegex.test(line)) {
            results.push({ lineNum: idx + 1, text: line, parsed: null, abnormal: null, isReturn: true });
        }
    });

    return results;
}

function renderNeedleGroups(items, needleCategory) {
    if (items.length === 0) {
        const icon = needleCategory === 'sample' ? 'fa-vial' : 'fa-flask';
        const title = needleCategory === 'sample' ? '样本针' : '试剂针';
        return '<div style="text-align:center;padding:40px 20px;color:#94a3b8;"><i class="fas ' + icon + '" style="font-size:2.5rem;opacity:0.3;margin-bottom:12px;"></i><div style="font-size:1rem;font-weight:600;">未找到' + title + '加注针监测数据</div></div>';
    }

    const abnormalItems = items.filter(i => i.abnormal && i.abnormal.abnormal);
    const cmdItems = items.filter(i => !i.isReturn);
    const cycleCount = items.filter(i => i.parsed && i.parsed.cmdType === 'B').length;
    const truncated = false;

    let html = '';
    html += '<div style="background:linear-gradient(135deg,#fef3c7 0%,#fde68a 100%);border-left:3px solid #f59e0b;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:0.82rem;color:#475569;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">';
    html += '<i class="fas fa-syringe" style="color:#f59e0b;"></i>';
    html += '<span>加注针监测: <strong>' + cycleCount + '</strong> 个监测周期, <strong>' + items.length + '</strong> 行</span>';
    if (abnormalItems.length > 0) {
        html += '<span style="color:#dc2626;font-weight:700;cursor:pointer;text-decoration:underline;" onclick="var e=document.getElementById(\'needle-abn-0\');if(e)e.scrollIntoView({behavior:\'smooth\',block:\'center\'})">⚠ 异常' + abnormalItems.length + '条(点击定位)</span>';
    } else if (cmdItems.length > 0) {
        html += '<span style="color:#16a34a;font-weight:600;">✓ 全部正常</span>';
    }
    if (truncated) {
        html += '<span style="color:#f59e0b;font-weight:600;">⚠ 仅显示前' + MAX_MODAL_DISPLAY + '行</span>';
    }
    html += '</div>';

    html += '<div style="max-height:calc(95vh - 200px);overflow-y:auto;">';
    const displayItems = items;
    let abnIdx = 0;
    let cycleNums = {};
    displayItems.forEach((item) => {
        const isAbn = item.abnormal && item.abnormal.abnormal;
        const isCycleStart = item.parsed && item.parsed.cmdType === 'B';
        const cleanText = item.text.replace(/\s+\d+\s*$/, '');

        if (isCycleStart) {
            const needleLabel = item.parsed.needleType || '加注针';
            cycleNums[needleLabel] = (cycleNums[needleLabel] || 0) + 1;
            const isFirst = Object.keys(cycleNums).length === 1 && cycleNums[needleLabel] === 1;
            if (!isFirst) {
                html += '<div style="height:8px;"></div>';
            }
            html += '<div style="margin:2px 0 4px;font-size:0.72rem;color:#6366f1;font-weight:600;letter-spacing:0.5px;">── ' + escapeHtml(needleLabel) + ' #' + cycleNums[needleLabel] + ' ──</div>';
        }

        let ruleText = '';
        let ruleColor = '#64748b';
        if (item.parsed && item.parsed.ok && item.parsed.fields && item.parsed.fields.desc) {
            const f = item.parsed.fields;
            ruleText = f.desc;
            if (f.label1) ruleText += ' | ' + f.label1 + ': ' + f.val1;
            if (f.label2) ruleText += ' | ' + f.label2 + ': ' + f.val2;
            if (f.label3) ruleText += ' | ' + f.label3 + ': ' + f.val3;
        } else if (item.parsed && !item.parsed.ok) {
            ruleText = '查询失败，数据无效';
            ruleColor = '#dc2626';
        } else if (item.isReturn) {
            const retMatch = item.text.match(/<[\dA-Fa-f-]+>\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})\s+([\dA-Fa-f]{2})/);
            if (retMatch) {
                const retCmd = retMatch[1].toUpperCase();
                if (retCmd === '0C' && retMatch[8]) {
                    const height = parseInt(retMatch[3] + retMatch[4], 16);
                    const staticVal = parseInt(retMatch[5] + retMatch[6], 16);
                    const diff = parseInt(retMatch[7] + retMatch[8], 16);
                    ruleText = '样本针返回 | 探液高度:' + height + ' (0x' + retMatch[3] + retMatch[4] + ') | 探液静态值:' + staticVal + ' (0x' + retMatch[5] + retMatch[6] + ') | 液位探测差值:' + diff + ' (0x' + retMatch[7] + retMatch[8] + ')';
                } else if (retCmd === '0D' && retMatch[8]) {
                    const height = parseInt(retMatch[3] + retMatch[4], 16);
                    const staticVal = parseInt(retMatch[5] + retMatch[6], 16);
                    const diff = parseInt(retMatch[7] + retMatch[8], 16);
                    ruleText = '试剂针返回 | 探液高度:' + height + ' (0x' + retMatch[3] + retMatch[4] + ') | 探液静态值:' + staticVal + ' (0x' + retMatch[5] + retMatch[6] + ') | 液位探测差值:' + diff + ' (0x' + retMatch[7] + retMatch[8] + ')';
                } else {
                    ruleText = '返回码:' + retMatch[1] + ' | 数据:' + retMatch.slice(2).join(' ');
                }
            } else {
                ruleText = '设备返回响应';
            }
            ruleColor = '#94a3b8';
        }

        const borderLeft = item.parsed ? ({ 'B': '3px solid #6366f1', 'C': '3px solid #0ea5e9', 'D': '3px solid #10b981', 'E': '3px solid #f59e0b' }[item.parsed.cmdType] || '3px solid #e2e8f0') : '3px solid #94a3b8';
        const bg = isAbn ? '#fef2f2' : (item.isReturn ? '#f9fafb' : '#fff');
        const idAttr = isAbn ? ' id="needle-abn-' + abnIdx + '"' : '';
        if (isAbn) abnIdx++;

        html += '<div' + idAttr + ' style="padding:4px 10px;margin:2px 0;background:' + bg + ';border-left:' + borderLeft + ';border-radius:0 4px 4px 0;font-family:monospace;font-size:0.8rem;line-height:1.6;white-space:pre-wrap;word-break:break-word;">';
        html += escapeHtml(cleanText);
        if (ruleText) {
            html += '  <span style="color:' + (isAbn ? '#dc2626' : ruleColor) + ';font-family:sans-serif;font-size:0.74rem;">' + escapeHtml(ruleText) + '</span>';
        }
        if (isAbn) {
            html += '  <span style="color:#dc2626;font-weight:600;font-family:sans-serif;font-size:0.74rem;">⚠ ' + escapeHtml(item.abnormal.reason) + '</span>';
        }
        html += '</div>';
    });
    if (truncated) {
        html += '<div style="text-align:center;padding:10px;color:#94a3b8;font-size:0.8rem;">⋯ 共 ' + items.length + ' 行，仅显示前 ' + MAX_MODAL_DISPLAY + ' 行 ⋯</div>';
    }
    html += '</div>';

    return html;
}


async function openFaultModal(type) {
    if (!currentFileName) { alert('请先选择一个文件'); return; }
    const config = FAULT_KEYWORDS[type];
    if (!config) return;
    let fullText = '';
    let fetchError = '';
    try {
        const resp = await fetch(`/api/analysis/${ANALYSIS_ID}/file?name=${encodeURIComponent(currentFileName)}`);
        if (resp.ok) {
            const data = await resp.json();
            fullText = data.content || '';
            _fullFileContent = fullText;
        } else {
            fetchError = 'HTTP ' + resp.status;
        }
    } catch(e) { fetchError = e.message; }
    if (!fullText || fullText.length < 10) {
        console.warn('openFaultModal: API获取失败(' + fetchError + ')，尝试缓存');
        fullText = _fullFileContent || getCurrentContent();
    }
    if (!fullText || fullText.length < 10) { alert('无法获取文件内容: ' + fetchError); return; }
    console.log('openFaultModal: fullText=' + fullText.length + ' chars, ' + fullText.split('\n').length + ' lines');
    const lines = fullText.split('\n');
    const modal = document.getElementById('fullModal');

    if (type === 'cooling') {
        const deviceModel = (_embeddedData && _embeddedData.model) || '';
        const isSmart6500 = deviceModel.toUpperCase().includes('6500');
        const isSmart500 = !isSmart6500 && deviceModel.toUpperCase().includes('500');
        const coolingMatches = [];
        const coolingRegex = isSmart500 ? /<\d+-\d+>\s+[01]C\s+[\dA-Fa-f]{2}/i : /<\d+-\d+>\s+0C\s+[\dA-Fa-f]{2}/i;
        lines.forEach((line, idx) => {
            if (coolingRegex.test(line)) {
                coolingMatches.push({ lineNum: idx + 1, text: line });
            }
        });
        document.getElementById('modalTitle').innerHTML = `<i class="fas fa-snowflake" style="margin-right:8px;color:#0ea5e9;"></i>制冷故障分析 - ${escapeHtml(currentFileName)} <span style="font-size:0.8rem;color:#64748b;">(${deviceModel || '未知型号'} | ${coolingMatches.length}条制冷命令)</span>`;
        let html = '';
        if (coolingMatches.length === 0) {
            html = `<div style="text-align:center;padding:60px 20px;color:#94a3b8;"><i class="fas fa-snowflake" style="font-size:3rem;opacity:0.3;margin-bottom:16px;"></i><div style="font-size:1.1rem;font-weight:600;">未找到0C/1C制冷命令记录</div><div style="margin-top:8px;font-size:0.85rem;">搜索命令格式: &lt;00-88&gt; 0C/1C ...</div></div>`;
        } else {
            const displayItems = coolingMatches;
            const isFail = (parsed) => {
                if (!parsed) return false;
                if (parsed.flagInfo && parsed.flagInfo.flagItems.some(f => !f.on)) return true;
                if (parsed.coolState && !parsed.coolState.normal) return true;
                if (parsed.overTemp && parsed.overTemp.length > 0) return true;
                return false;
            };
            let failCount = 0, firstFailIdx = -1;
            displayItems.forEach((m, i) => {
                const p = isSmart500 ? (parseCoolingLine500_0C(m.text) || parseCoolingLine500_1C(m.text)) : parseCoolingLine(m.text, 'SMART6500');
                if (isFail(p)) { failCount++; if (firstFailIdx === -1) firstFailIdx = i; }
            });
            let failInfo = '';
            if (failCount > 0) {
                failInfo = `，<span style="color:#dc2626;font-weight:700;cursor:pointer;text-decoration:underline;" onclick="document.getElementById('cooling-item-${firstFailIdx}')&&document.getElementById('cooling-item-${firstFailIdx}').scrollIntoView({behavior:'smooth',block:'center'})">异常${failCount}条(点击定位)</span>`;
            } else {
                failInfo = '，<span style="color:#16a34a;font-weight:600;">全部正常</span>';
            }
            html = `<div style="background:linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);border-left:3px solid #0ea5e9;border-radius:8px;padding:10px;margin-bottom:12px;font-size:0.85rem;color:#475569;"><i class="fas fa-info-circle" style="margin-right:6px;"></i>共找到 <strong style="color:#0ea5e9;">${coolingMatches.length}</strong> 条制冷命令${failInfo}，以下逐条解析字段含义</div>`;
            html += '<div style="max-height:calc(95vh - 160px);overflow-y:auto;">';

            displayItems.forEach((m, i) => {
                const parsed = isSmart500 ? (parseCoolingLine500_0C(m.text) || parseCoolingLine500_1C(m.text)) : parseCoolingLine(m.text, 'SMART6500');
                const itemFail = isFail(parsed);
                html += `<div id="cooling-item-${i}" style="margin:6px 0;background:white;border-radius:8px;border:1px solid ${itemFail ? '#fecaca' : '#e2e8f0'};box-shadow:0 1px 3px rgba(0,0,0,0.05);overflow:hidden;">`;
                html += `<div style="display:flex;gap:8px;padding:6px 10px;background:${itemFail ? '#fef2f2' : '#f8fafc'};border-bottom:1px solid ${itemFail ? '#fecaca' : '#e2e8f0'};"><span style="color:#94a3b8;font-size:0.75rem;min-width:40px;text-align:right;flex-shrink:0;padding-top:2px;">L${m.lineNum}</span><span style="font-family:monospace;font-size:0.82rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;flex:1;color:#334155;">${escapeHtml(m.text)}</span></div>`;
                if (parsed) {
                    html += `<div style="padding:8px 12px;font-size:0.82rem;line-height:1.8;">`;
                    html += `<div style="display:grid;grid-template-columns:100px 1fr;gap:2px 8px;">`;
                    html += `<span style="color:#64748b;font-weight:500;">系列:</span><span style="color:#6366f1;font-weight:600;">${parsed.series || 'SMART6500'}</span>`;
                    html += `<span style="color:#64748b;font-weight:500;">命令:</span><span style="font-weight:600;">${parsed.cmd}</span>`;
                    html += `<span style="color:#64748b;font-weight:500;">地址:</span><span>${parsed.addr}</span>`;
                    html += `<span style="color:#64748b;font-weight:500;">查询状态:</span><span style="color:${parsed.status === '查询成功' ? '#16a34a' : '#dc2626'};font-weight:600;">${parsed.status}</span>`;
                    if (parsed.dataValid === false) {
                        html += `<span style="color:#dc2626;font-weight:500;">提示:</span><span style="color:#dc2626;font-size:0.78rem;">查询失败，数据字节无效，温度/旗标/PWM不予解析</span>`;
                    }
                    if (parsed.cmd === '0C' && parsed.series === 'SMART6500') {
                        html += `<span style="color:#64748b;font-weight:500;">试剂制冷温度:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.reagentTemp}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">C2传感器温度:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.sensorTemp}</span>`;
                        if (parsed.flagInfo) {
                            const flagHtml = parsed.flagInfo.flagItems.map(f => f.on ? `<span style="color:#16a34a;">${f.text}</span>` : `<span style="color:#dc2626;font-weight:700;background:#fef2f2;padding:1px 4px;border-radius:3px;">${f.text}</span>`).join('，');
                            html += `<span style="color:#64748b;font-weight:500;">旗标位:</span><span>${parsed.flagInfo.raw}(0x${parsed.flagInfo.val.toString(16).padStart(2,'0')}) → ${flagHtml}</span>`;
                            html += `<span style="color:#64748b;font-weight:500;">PWM功率:</span><span style="color:#f59e0b;font-weight:600;">${parsed.pwm}</span>`;
                        }
                    } else if (parsed.cmd === '0C' && parsed.series === 'SMART500') {
                        html += `<span style="color:#64748b;font-weight:500;">试剂制冷温度:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.reagentTemp}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">冷端温度:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.coldEndTemp}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">热端温度:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.hotEndTemp}</span>`;
                        if (parsed.overTemp && parsed.overTemp.length > 0) {
                            html += `<span style="color:#64748b;font-weight:500;">过温保护:</span><span style="color:#dc2626;font-weight:700;background:#fef2f2;padding:1px 4px;border-radius:3px;">${parsed.overTemp.join('，')}</span>`;
                        }
                    } else if (parsed.cmd === '1C' && parsed.series === 'SMART500') {
                        html += `<span style="color:#64748b;font-weight:500;">PWM功率:</span><span style="color:#f59e0b;font-weight:600;">${parsed.pwm}</span>`;
                        const stateColor = parsed.coolState.normal ? '#16a34a' : '#dc2626';
                        const stateBg = parsed.coolState.normal ? '' : 'background:#fef2f2;padding:1px 4px;border-radius:3px;';
                        html += `<span style="color:#64748b;font-weight:500;">制冷状态:</span><span style="color:${stateColor};font-weight:700;${stateBg}">${parsed.coolState.text}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">制冷片电压:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.tecVolt}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">制冷片电流:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.tecCurr}</span>`;
                        html += `<span style="color:#64748b;font-weight:500;">水冷泵转速:</span><span style="color:#0ea5e9;font-weight:600;">${parsed.pumpSpeed}</span>`;
                    }
                    html += `</div></div>`;
                } else {
                    html += `<div style="padding:6px 12px;font-size:0.8rem;color:#94a3b8;"><i class="fas fa-exclamation-triangle" style="margin-right:4px;"></i>格式不匹配，无法解析</div>`;
                }
                html += `</div>`;
            });
            html += '</div>';
        }
        document.getElementById('modalBody').innerHTML = html;
        modal.style.display = 'flex';
        return;
    }
    const matches = [];
    const lowerKeywords = config.keywords.map(k => k.toLowerCase());
    lines.forEach((line, idx) => {
        const lowerLine = line.toLowerCase();
        for (const kw of lowerKeywords) {
            if (lowerLine.includes(kw)) {
                matches.push({ lineNum: idx + 1, text: line, keyword: kw });
                break;
            }
        }
    });

    let needleHtml = '';
    let needleCount = 0;
    if (type === 'sample' || type === 'reagent') {
        const deviceModel = (_embeddedData && _embeddedData.model) || '';
        const series = deviceModel.toUpperCase().includes('6500') ? 'SMART6500' : 'SMART500';
        const needleGroups = groupNeedleData(lines, type, series);
        needleCount = needleGroups.length;
        needleHtml = renderNeedleGroups(needleGroups, type);
    }

    document.getElementById('modalTitle').innerHTML = `<i class="fas ${config.icon}" style="margin-right:8px;color:${config.color};"></i>${config.title} - ${escapeHtml(currentFileName)} <span style="font-size:0.8rem;color:#64748b;">(${matches.length}条关键词${needleCount > 0 ? ' | ' + needleCount + '组加注针监测' : ''})</span>`;
    let html = '';
    if (needleHtml) {
        html += needleHtml;
        if (matches.length > 0) {
            html += '<div style="margin:12px 0 8px;text-align:center;color:#94a3b8;font-size:0.8rem;"><i class="fas fa-grip-lines" style="margin-right:4px;"></i>以下为关键词匹配记录</div>';
        }
    }
    if (matches.length === 0) {
        if (!needleHtml) {
            html = `<div style="text-align:center;padding:60px 20px;color:#94a3b8;"><i class="fas ${config.icon}" style="font-size:3rem;opacity:0.3;margin-bottom:16px;"></i><div style="font-size:1.1rem;font-weight:600;">未找到${config.title}相关记录</div><div style="margin-top:8px;font-size:0.85rem;">搜索关键词: ${config.keywords.join(', ')}</div></div>`;
        }
    } else {
        html += `<div style="background:linear-gradient(135deg, #f0f9ff 0%, #e0f2fe 100%);border-left:3px solid ${config.color};border-radius:8px;padding:10px;margin-bottom:12px;font-size:0.85rem;color:#475569;"><i class="fas fa-info-circle" style="margin-right:6px;"></i>共找到 <strong style="color:${config.color};">${matches.length}</strong> 条关键词匹配记录</div>`;
        html += '<div style="max-height:calc(95vh - 160px);overflow-y:auto;">';
        const kwRegex = new RegExp('(' + config.keywords.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
        matches.forEach((m, i) => {
            const highlighted = escapeHtml(m.text).replace(kwRegex, '<mark style="background:#fef08a;padding:1px 3px;border-radius:2px;">$1</mark>');
            html += `<div style="display:flex;gap:8px;padding:6px 10px;margin:4px 0;background:white;border-radius:6px;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,0.05);"><span style="color:#94a3b8;font-size:0.75rem;min-width:40px;text-align:right;flex-shrink:0;padding-top:2px;">L${m.lineNum}</span><span style="font-family:monospace;font-size:0.85rem;line-height:1.5;white-space:pre-wrap;word-break:break-word;flex:1;">${highlighted}</span></div>`;
        });
        html += '</div>';
    }
    document.getElementById('modalBody').innerHTML = html;
    modal.style.display = 'flex';
}

function openMatchModal(groupId, title) {
    const modal = document.getElementById('fullModal');
    const icon = groupId === 'sampleMatches' ? 'fa-vial' : 'fa-flask';
    document.getElementById('modalTitle').innerHTML = `<i class="fas ${icon}" style="margin-right:8px;"></i>${title} - 全文查看`;
    const items = window.matchData[groupId] || [];
    const displayItems = items;
    const bgColor = groupId === 'sampleMatches' ? '#e0f2fe' : '#f3e8ff';
    const borderColor = groupId === 'sampleMatches' ? 'var(--info)' : 'var(--purple)';
    let contentHtml = '';
    displayItems.forEach((item, idx) => {
        contentHtml += `<div style="background:linear-gradient(135deg, #fefce8 0%, #fef9c3 100%);border-left:3px solid var(--warning);padding:6px 10px;margin:2px 0;border-radius:6px;font-family:monospace;font-size:0.92rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;box-shadow:var(--shadow-sm);"><i class="fas fa-file-alt" style="color:var(--warning);margin-right:6px;"></i>${escapeHtml(item.original_text || '')}</div>`;
        contentHtml += `<div style="color:var(--success);padding:4px 10px;font-size:0.92rem;line-height:1.4;background:rgba(16,185,129,0.05);border-radius:4px;margin:2px 0;"><i class="fas fa-lightbulb" style="margin-right:6px;"></i><strong>诊断建议:</strong> ${escapeHtml(item.advice || '')}</div>`;
        if (idx < items.length - 1) {
            contentHtml += `<div style="height:8px;"></div>`;
        }
    });
    let html = `<div style="background:${bgColor};border-left:3px solid ${borderColor};border-radius:8px;padding:10px;max-height:calc(95vh - 120px);overflow-y:auto;box-shadow:var(--shadow);">${contentHtml}</div>`;
    document.getElementById('modalBody').innerHTML = html || '<div style="text-align:center;color:var(--gray-600);padding:40px;"><i class="fas fa-inbox" style="font-size:2rem;opacity:0.5;margin-bottom:10px;"></i><br>暂无匹配数据</div>';
    modal.style.display = 'flex';
}

function filterTree() {
    searchQuery = document.getElementById('searchInput').value.toLowerCase().trim();
    const allDateGroups = document.querySelectorAll('.date-group');
    allDateGroups.forEach(group => {
        let hasVisible = false;
        const fileNodes = group.querySelectorAll('.file-node');
        fileNodes.forEach(node => {
            const searchable = node.getAttribute('data-searchable') || '';
            if (!searchQuery || searchable.includes(searchQuery)) {
                node.style.display = '';
                hasVisible = true;
            } else {
                node.style.display = 'none';
            }
        });
        const dateHeader = group.querySelector('.date-header');
        if (dateHeader) {
            dateHeader.style.display = hasVisible ? '' : 'none';
        }
    });
}

function matchesSearch(filename) {
    if (!searchQuery) return true;
    return filename.toLowerCase().includes(searchQuery);
}

function escapeHtml(text) {
    if (!text) return '';
    return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

function escapeAttr(text) {
    if (!text) return '';
    return text.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#039;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

let searchMatches = [];
let currentMatchIndex = -1;
let markedLines = new Set();
let bookmarks = new Set();
let _lastFindNextParams = null;

function openSearchDialog() {
    document.getElementById('searchDialog').style.display = 'flex';
    document.getElementById('findInput').focus();
    initDialogDrag();
}

function closeSearchDialog() {
    document.getElementById('searchDialog').style.display = 'none';
}

let dialogDragInit = false;
function initDialogDrag() {
    if (dialogDragInit) return;
    dialogDragInit = true;
    
    const handle = document.getElementById('dialogDragHandle');
    const dialog = document.getElementById('searchDialogBox');
    let isDragging = false, startY, startHeight;
    
    handle.addEventListener('mousedown', (e) => {
        isDragging = true;
        startY = e.clientY;
        startHeight = dialog.offsetHeight;
        document.body.style.userSelect = 'none';
    });
    
    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        const newHeight = Math.min(Math.max(startHeight + startY - e.clientY, 350), window.innerHeight * 0.9);
        dialog.style.height = newHeight + 'px';
    });
    
    document.addEventListener('mouseup', () => {
        isDragging = false;
        document.body.style.userSelect = '';
    });
}

function switchSearchTab(tabName) {
    document.querySelectorAll('.search-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.search-tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector(`.search-tab[data-tab="${tabName}"]`).classList.add('active');
    document.getElementById(`tab-${tabName}`).classList.add('active');
}

function getCurrentContent() {
    if (_fullFileContent != null) return _fullFileContent;
    const rightBody = document.getElementById('rightBody');
    if (!rightBody) return '';
    const rawContent = rightBody.querySelector('.raw-content');
    return rawContent ? rawContent.textContent : rightBody.textContent;
}

function buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord) {
    let flags = caseSensitive ? 'g' : 'gi';
    let searchPattern = pattern;
    
    if (!isRegex) {
        searchPattern = searchPattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }
    
    if (wholeWord) {
        searchPattern = `\\b${searchPattern}\\b`;
    }
    
    try {
        return new RegExp(searchPattern, flags);
    } catch (e) {
        return null;
    }
}

function findPrev() {
    if (!searchMatches.length) {
        findNext();
        return;
    }
    currentMatchIndex--;
    if (currentMatchIndex < 0) currentMatchIndex = searchMatches.length - 1;
    highlightCurrentMatch();
    showStatus(`第 ${currentMatchIndex + 1} / ${searchMatches.length} 个匹配项`);
}

function findNext() {
    const pattern = document.getElementById('findInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('findRegex').checked;
    const caseSensitive = document.getElementById('findCaseSensitive').checked;
    const wholeWord = document.getElementById('findWholeWord').checked;
    const wrap = document.getElementById('findWrap').checked;
    
    const paramsKey = pattern + '|' + isRegex + '|' + caseSensitive + '|' + wholeWord;
    const cached = _lastFindNextParams === paramsKey && searchMatches.length > 0;
    
    if (!cached) {
        const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
        if (!regex) {
            showStatus('正则表达式错误');
            return;
        }
        
        const content = getCurrentContent();
        if (content.length > 5000000) { showStatus('文件过大(>5MB)，请使用全局搜索'); return; }
        const lines = content.split('\n');
        
        searchMatches = [];
        const MAX_MATCHES = 5000;
        for (let idx = 0; idx < lines.length && searchMatches.length < MAX_MATCHES; idx++) {
            const line = lines[idx];
            if (line.length === 0) continue;
            let match;
            regex.lastIndex = 0;
            while ((match = regex.exec(line)) !== null) {
                searchMatches.push({
                    lineNum: idx + 1,
                    col: match.index,
                    text: line.length > 300 ? line.substring(0, 300) : line,
                    match: match[0]
                });
                if (searchMatches.length >= MAX_MATCHES) break;
            }
        }
        _lastFindNextParams = paramsKey;
        currentMatchIndex = -1;
        
        if (searchMatches.length === 0) {
            showStatus('未找到匹配项');
            return;
        }
    }
    
    currentMatchIndex++;
    if (currentMatchIndex >= searchMatches.length) {
        if (wrap) {
            currentMatchIndex = 0;
        } else {
            currentMatchIndex = searchMatches.length - 1;
            showStatus('已到达最后一个匹配项');
            return;
        }
    }
    
    highlightCurrentMatch();
    showStatus(`第 ${currentMatchIndex + 1} / ${searchMatches.length} 个匹配项`);
}

let _lineOffsetsCache = null;
let _lineOffsetsCacheLen = -1;

function getLineStartOffset(lineNum) {
    const fullText = _fullFileContent || '';
    if (_lineOffsetsCacheLen !== fullText.length) {
        _lineOffsetsCache = [0];
        for (let i = 0; i < fullText.length; i++) {
            if (fullText.charCodeAt(i) === 10) _lineOffsetsCache.push(i + 1);
        }
        _lineOffsetsCacheLen = fullText.length;
    }
    return _lineOffsetsCache[lineNum - 1] !== undefined ? _lineOffsetsCache[lineNum - 1] : -1;
}

function highlightCurrentMatch() {
    if (currentMatchIndex < 0 || currentMatchIndex >= searchMatches.length) return;
    
    const match = searchMatches[currentMatchIndex];
    const rightBody = document.getElementById('rightBody');
    if (!rightBody) return;
    const rawContent = rightBody.querySelector('.raw-content');
    if (!rawContent) return;
    
    const prevHl = rawContent.querySelector('.highlight-current');
    if (prevHl) {
        const parent = prevHl.parentNode;
        parent.replaceChild(document.createTextNode(prevHl.textContent), prevHl);
        parent.normalize();
    }
    
    const lineEl = rawContent.querySelector(`[data-line="${match.lineNum}"]`);
    if (lineEl) {
        const regex = new RegExp(`(${escapeRegExp(match.match)})`, 'gi');
        const frag = document.createDocumentFragment();
        let lastIdx = 0;
        const text = lineEl.textContent;
        let m;
        while ((m = regex.exec(text)) !== null) {
            if (m.index > lastIdx) frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
            const span = document.createElement('span');
            span.className = 'highlight-current';
            span.textContent = m[1];
            frag.appendChild(span);
            lastIdx = regex.lastIndex;
        }
        if (lastIdx < text.length) frag.appendChild(document.createTextNode(text.slice(lastIdx)));
        lineEl.textContent = '';
        lineEl.appendChild(frag);
        const highlightEl = lineEl.querySelector('.highlight-current');
        if (highlightEl) highlightEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
    }
    
    const fullText = _fullFileContent || rawContent.textContent;
    if (!fullText) return;
    
    const lineStart = getLineStartOffset(match.lineNum);
    if (lineStart < 0) return;
    const lineEnd = fullText.indexOf('\n', lineStart);
    const lineText = fullText.substring(lineStart, lineEnd === -1 ? fullText.length : lineEnd);
    
    const absStart = lineStart + (match.col || 0);
    const absEnd = absStart + (match.match ? match.match.length : 0);
    if (absEnd <= absStart) return;
    
    const walker = document.createTreeWalker(rawContent, NodeFilter.SHOW_TEXT, null);
    let curOffset = 0;
    let startNode = null, startOff = 0, endNode = null, endOff = 0;
    
    while (walker.nextNode()) {
        const node = walker.currentNode;
        const nodeLen = node.textContent.length;
        if (startNode === null && curOffset + nodeLen > absStart) {
            startNode = node;
            startOff = absStart - curOffset;
        }
        if (curOffset + nodeLen >= absEnd) {
            endNode = node;
            endOff = absEnd - curOffset;
            break;
        }
        curOffset += nodeLen;
    }
    
    if (startNode) {
        try {
            if (startNode === endNode) {
                const nodeText = startNode.textContent;
                const before = nodeText.substring(0, startOff);
                const matched = nodeText.substring(startOff, endOff);
                const after = nodeText.substring(endOff);
                const parent = startNode.parentNode;
                const frag = document.createDocumentFragment();
                if (before) frag.appendChild(document.createTextNode(before));
                const span = document.createElement('span');
                span.className = 'highlight-current';
                span.textContent = matched;
                frag.appendChild(span);
                if (after) frag.appendChild(document.createTextNode(after));
                parent.replaceChild(frag, startNode);
                span.scrollIntoView({ behavior: 'smooth', block: 'center' });
            } else {
                const range = document.createRange();
                range.setStart(startNode, startOff);
                range.setEnd(endNode, endOff);
                const span = document.createElement('span');
                span.className = 'highlight-current';
                range.surroundContents(span);
                span.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        } catch (e) {
            const lineHeight = 26;
            const targetScroll = Math.max(0, (match.lineNum - 1) * lineHeight - rawContent.clientHeight / 2);
            rawContent.scrollTo({ top: targetScroll, behavior: 'smooth' });
            showStatus(`行 ${match.lineNum}: ${lineText.substring(0, 100)}`);
        }
    }
}

function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function findAll() {
    const pattern = document.getElementById('findInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('findRegex').checked;
    const caseSensitive = document.getElementById('findCaseSensitive').checked;
    const wholeWord = document.getElementById('findWholeWord').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const content = getCurrentContent();
    if (content.length > 5000000) { showStatus('文件过大(>5MB)，请使用全局搜索'); return; }
    const lines = content.split('\n');
    
    searchMatches = [];
    const MAX_MATCHES = 5000;
    
    for (let idx = 0; idx < lines.length && searchMatches.length < MAX_MATCHES; idx++) {
        const line = lines[idx];
        if (line.length === 0) continue;
        let match;
        regex.lastIndex = 0;
        while ((match = regex.exec(line)) !== null) {
            searchMatches.push({
                lineNum: idx + 1,
                col: match.index,
                text: line.length > 300 ? line.substring(0, 300) : line,
                match: match[0]
            });
            if (searchMatches.length >= MAX_MATCHES) break;
        }
    }
    
    if (!searchMatches.length) {
        showStatus('未找到匹配项');
        return;
    }
    _lastFindNextParams = null;
    currentMatchIndex = -1;
    
    // 打开底部结果面板
    openResultPanel();
    closeSearchDialog();
    
    // 生成结果组ID
    const gid = ++resultGroupId;
    
    // 构建结果项HTML
    let itemsArr = [];
    const displayMatches = searchMatches.slice(0, MAX_SEARCH_DISPLAY);
    displayMatches.forEach(m => {
        const highlighted = escapeHtml(m.text.substring(0, 150)).replace(
            new RegExp(`(${escapeRegExp(escapeHtml(m.match))})`, 'gi'),
            '<span class="match-highlight">$1</span>'
        );
        itemsArr.push(`<div class="result-item-mini" onclick="jumpToLine(${m.lineNum})">
            <span class="line-num">行 ${m.lineNum}:</span>
            <span class="line-text">${highlighted}</span>
        </div>`);
    });
    const itemsHtml = itemsArr.join("");
    
    // 构建结果组HTML
    const moreBtn = '';
    const groupHtml = `
        <div class="result-group" id="group-${gid}">
            <div class="result-group-header" onclick="toggleResultGroup(${gid})">
                <span class="result-group-title">
                    <i class="fas fa-search"></i>
                    "${escapeHtml(pattern)}" (${searchMatches.length}个匹配)
                </span>
                <div class="result-group-actions">
                    <button class="result-toggle-btn" id="toggle-${gid}" onclick="event.stopPropagation();toggleResultGroup(${gid})">−</button>
                    <button class="result-close-btn" onclick="event.stopPropagation();removeResultGroup(${gid})"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <div class="result-group-content" id="content-${gid}">
                ${itemsHtml}
                ${moreBtn}
            </div>
            <div class="result-group-resize" onmousedown="startGroupResize(event, ${gid})"></div>
        </div>
    `;
    
    // 添加到面板（最新的在上面）
    if (!window._searchResultsCache) window._searchResultsCache = {};
    window._searchResultsCache[gid] = searchMatches;
    document.getElementById('resultPanelBody').insertAdjacentHTML('afterbegin', groupHtml);
    
    showStatus(`找到 ${searchMatches.length} 个匹配项`);
}

function countMatches() {
    const pattern = document.getElementById('findInput').value;
    if (!pattern) {
        showStatus('请输入查找内容');
        return;
    }
    
    const isRegex = document.getElementById('findRegex').checked;
    const caseSensitive = document.getElementById('findCaseSensitive').checked;
    const wholeWord = document.getElementById('findWholeWord').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const content = getCurrentContent();
    if (!content) {
        showStatus('请先选择一个文件');
        return;
    }
    
    const matches = content.match(regex);
    const count = matches ? matches.length : 0;
    
    showStatus(`共 ${count} 个匹配项`);
}

function highlightAll() {
    const pattern = document.getElementById('findInput').value;
    if (!pattern) {
        showStatus('请输入查找内容');
        return;
    }
    
    const isRegex = document.getElementById('findRegex').checked;
    const caseSensitive = document.getElementById('findCaseSensitive').checked;
    const wholeWord = document.getElementById('findWholeWord').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const rightBody = document.getElementById('rightBody');
    if (!rightBody) {
        showStatus('请先选择一个文件');
        return;
    }
    
    const rawContent = rightBody.querySelector('.raw-content');
    
    if (rawContent) {
        const content = rawContent.innerHTML;
        if (content.length > 500000) { showStatus('文件过大(>500KB)，无法全量高亮，请使用查找功能'); return; }
        const highlighted = content.replace(regex, '<span class="highlight-mark">$&</span>');
        rawContent.innerHTML = highlighted;
        showStatus('已高亮所有匹配项');
    } else {
        showStatus('请先选择一个文件');
    }
}

function replaceNext() {
    const pattern = document.getElementById('replaceFindInput').value;
    const replacement = document.getElementById('replaceWithInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('replaceRegex').checked;
    const caseSensitive = document.getElementById('replaceCaseSensitive').checked;
    const wholeWord = document.getElementById('replaceWholeWord').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const rightBody = document.getElementById('rightBody');
    const rawContent = rightBody.querySelector('.raw-content');
    
    if (rawContent) {
        const content = rawContent.textContent;
        const match = content.match(regex);
        
        if (match) {
            const onceRegex = new RegExp(regex.source, regex.flags.replace('g', ''));
            const newContent = content.replace(onceRegex, replacement);
            rawContent.innerHTML = escapeHtml(newContent);
            showStatus('已替换 1 处');
        } else {
            showStatus('未找到匹配项');
        }
    }
}

function replaceAll() {
    const pattern = document.getElementById('replaceFindInput').value;
    const replacement = document.getElementById('replaceWithInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('replaceRegex').checked;
    const caseSensitive = document.getElementById('replaceCaseSensitive').checked;
    const wholeWord = document.getElementById('replaceWholeWord').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const rightBody = document.getElementById('rightBody');
    const rawContent = rightBody.querySelector('.raw-content');
    
    if (rawContent) {
        const content = rawContent.textContent;
        const matches = content.match(regex);
        const count = matches ? matches.length : 0;
        
        if (count > 0) {
            const newContent = content.replace(regex, replacement);
            rawContent.innerHTML = escapeHtml(newContent);
            showStatus(`已替换 ${count} 处`);
        } else {
            showStatus('未找到匹配项');
        }
    }
}

function findInFiles() {
    const pattern = document.getElementById('findInFilesInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('findInFilesRegex').checked;
    const caseSensitive = document.getElementById('findInFilesCaseSensitive').checked;
    const filePattern = document.getElementById('filePatternInput').value || '*';
    
    showStatus('正在搜索...');
    
    fetch('/api/search-in-files', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': CSRF_TOKEN
        },
        body: JSON.stringify({
            pattern: pattern,
            is_regex: isRegex,
            case_sensitive: caseSensitive,
            file_pattern: filePattern,
            analysis_id: ANALYSIS_ID
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.results) {
            const results = data.results.map(r => 
                `<div class="search-result-item" onclick="jumpToFile('${escapeHtml(r.file).replace(/'/g, "\\'")}', ${r.line})">
                    <span class="line-num">${escapeHtml(r.file)} 行 ${r.line}:</span>
                    <span class="line-text">${escapeHtml(r.text)}</span>
                </div>`
            ).join('');
            document.getElementById('findInFilesResults').innerHTML = results;
            showStatus(`在 ${data.file_count} 个文件中找到 ${data.total_matches} 个匹配项`);
        } else {
            showStatus(data.error || '搜索失败');
        }
    })
    .catch(e => {
        showStatus('搜索出错: ' + e.message);
    });
}

function markAll() {
    const pattern = document.getElementById('markInput').value;
    if (!pattern) return;
    
    const isRegex = document.getElementById('markRegex').checked;
    const caseSensitive = document.getElementById('markCaseSensitive').checked;
    const wholeWord = document.getElementById('markWholeWord').checked;
    const bookmark = document.getElementById('markBookmark').checked;
    
    const regex = buildSearchPattern(pattern, isRegex, caseSensitive, wholeWord);
    if (!regex) {
        showStatus('正则表达式错误');
        return;
    }
    
    const content = getCurrentContent();
    if (content.length > 5000000) { showStatus('文件过大(>5MB)，请使用全局搜索'); return; }
    const lines = content.split('\n');
    
    markedLines.clear();
    const results = [];
    
    lines.forEach((line, idx) => {
        regex.lastIndex = 0;
        if (regex.test(line)) {
            markedLines.add(idx + 1);
            if (bookmark) {
                bookmarks.add(idx + 1);
            }
    if (results.length < MAX_SEARCH_DISPLAY) results.push(`<div class="search-result-item" onclick="jumpToLine(${idx + 1})">
                <span class="line-num">行 ${idx + 1}:</span>
                <span class="line-text">${escapeHtml(line)}</span>
            </div>`);
        }
    });
    
    document.getElementById('markResults').innerHTML = results.join('');
    showStatus(`已标记 ${markedLines.size} 行${bookmark ? `，${bookmarks.size} 个书签` : ''}`);
}

function clearMarks() {
    markedLines.clear();
    bookmarks.clear();
    
    const rightBody = document.getElementById('rightBody');
    const rawContent = rightBody.querySelector('.raw-content');
    
    if (rawContent) {
        const content = rawContent.textContent;
        rawContent.textContent = content;
    }
    
    document.getElementById('markResults').innerHTML = '';
    showStatus('已清除所有标记');
}

function jumpToLine(lineNum) {
    const rightBody = document.getElementById('rightBody');
    const rawContent = rightBody && rightBody.querySelector('.raw-content');
    if (!rawContent) return;
    if (rawContent.innerHTML.length > 500000) {
        const targetLine = _fullFileContent ? _fullFileContent.split('\n')[lineNum-1] : null;
        if (targetLine) showStatus('行 ' + lineNum + ': ' + targetLine.substring(0, 200));
        return;
    }
    const fullText = _fullFileContent || rawContent.textContent;
    if (!fullText) return;
    const allLines = fullText.split('\n');
    if (lineNum < 1 || lineNum > allLines.length) return;
    const lineHeight = 23;
    rawContent.scrollTop = Math.max(0, (lineNum - 1) * lineHeight - rawContent.clientHeight / 2);
    showStatus('行 ' + lineNum + ': ' + allLines[lineNum-1].substring(0, 100));
}

function jumpToFile(filename, line) {
    const fileNodes = document.querySelectorAll('.file-node');
    for (const node of fileNodes) {
        if (node.textContent.includes(filename)) {
            node.click();
            setTimeout(() => jumpToLine(line), 500);
            break;
        }
    }
}

function loadMoreSearchResults(gid, pattern, currentOffset) {
    const allMatches = window._searchResultsCache && window._searchResultsCache[gid];
    if (!allMatches) return;
    const batchSize = 500;
    const nextBatch = allMatches.slice(currentOffset, currentOffset + batchSize);
    const contentEl = document.getElementById('content-' + gid);
    const moreBtn = document.getElementById('searchMore-' + gid);
    if (!contentEl || !moreBtn) return;
    let html = '';
    nextBatch.forEach(m => {
        const highlighted = escapeHtml(m.text.substring(0, 150)).replace(
            new RegExp('(' + escapeRegExp(escapeHtml(m.match)) + ')', 'gi'),
            '<span class="match-highlight">$1</span>'
        );
        html += '<div class="result-item-mini" onclick="jumpToLine(' + m.lineNum + ')"><span class="line-num">行 ' + m.lineNum + ':</span><span class="line-text">' + highlighted + '</span></div>';
    });
    const newOffset = currentOffset + nextBatch.length;
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = html;
    while (tempDiv.firstChild) { contentEl.insertBefore(tempDiv.firstChild, moreBtn); }
    if (newOffset < allMatches.length) {
        moreBtn.innerHTML = '<i class="fas fa-angle-double-down" style="margin-right:4px;"></i>显示更多 (已显示 ' + newOffset + '/' + allMatches.length + ')';
        moreBtn.onclick = function() { loadMoreSearchResults(gid, pattern, newOffset); };
    } else {
        moreBtn.innerHTML = '<i class="fas fa-check-circle" style="margin-right:4px;color:#22c55e;"></i>全部显示完成 (共' + allMatches.length + '条)';
        moreBtn.onclick = null;
        moreBtn.style.cursor = 'default';
    }
}

function showStatus(message) {
    document.getElementById('searchStatus').textContent = message;
}

document.addEventListener('keydown', function(e) {
    if (e.ctrlKey && e.key === 'f') {
        e.preventDefault();
        openSearchDialog();
    } else if (e.key === 'F3') {
        e.preventDefault();
        if (e.shiftKey) {
            findPrev();
        } else {
            findNext();
        }
    } else if (e.ctrlKey && e.key === 'm') {
        e.preventDefault();
        switchSearchTab('mark');
        openSearchDialog();
    } else if (e.key === 'Escape') {
        const dialog = document.getElementById('searchDialog');
        if (dialog && dialog.style.display !== 'none') {
            const active = document.activeElement;
            const inputs = dialog.querySelectorAll('input, textarea, select');
            let inInput = false;
            inputs.forEach(inp => { if (inp === active) inInput = true; });
            if (!inInput) closeSearchDialog();
        }
    }
});

loadAnalysisData();



// 结果面板相关
let resultGroupId = 0;
let resultPanelHeight = 300;

function initResultPanelDrag() {
    const handle = document.getElementById('panelDragHandle');
    const panel = document.getElementById('searchResultPanel');
    let isDragging = false, startY, startHeight;
    
    function onStart(y) {
        isDragging = true;
        startY = y;
        startHeight = panel.offsetHeight;
        document.body.style.userSelect = 'none';
        handle.style.background = 'linear-gradient(135deg, #9ca3af, #6b7280)';
    }
    
    function onMove(y) {
        if (!isDragging) return;
        const newHeight = Math.min(Math.max(startHeight + startY - y, 150), window.innerHeight * 0.85);
        panel.style.height = newHeight + 'px';
        resultPanelHeight = newHeight;
    }
    
    function onEnd() {
        if (!isDragging) return;
        isDragging = false;
        document.body.style.userSelect = '';
        handle.style.background = '';
    }
    
    handle.addEventListener('mousedown', (e) => { e.preventDefault(); onStart(e.clientY); });
    document.addEventListener('mousemove', (e) => { onMove(e.clientY); });
    document.addEventListener('mouseup', () => { onEnd(); });
    
    handle.addEventListener('touchstart', (e) => { e.preventDefault(); onStart(e.touches[0].clientY); }, {passive: false});
    document.addEventListener('touchmove', (e) => { onMove(e.touches[0].clientY); });
    document.addEventListener('touchend', () => { onEnd(); });
}

function openResultPanel() {
    const panel = document.getElementById('searchResultPanel');
    panel.classList.add('show');
    panel.style.height = resultPanelHeight + 'px';
}

function closeResultPanel() {
    document.getElementById('searchResultPanel').classList.remove('show');
}

function toggleResultGroup(id) {
    const content = document.getElementById('content-' + id);
    const btn = document.getElementById('toggle-' + id);
    if (content.classList.contains('collapsed')) {
        content.classList.remove('collapsed');
        btn.textContent = '−';
    } else {
        content.classList.add('collapsed');
        btn.textContent = '+';
    }
}

function removeResultGroup(id) {
    if (window._searchResultsCache) delete window._searchResultsCache[id];
    const group = document.getElementById('group-' + id);
    if (group) group.remove();
    if (!document.getElementById('resultPanelBody').children.length) closeResultPanel();
}

function startGroupResize(e, gid) {
    e.preventDefault();
    e.stopPropagation();
    const content = document.getElementById('content-' + gid);
    if (!content || content.classList.contains('collapsed')) return;
    const startY = e.clientY;
    const startMaxHeight = parseInt(content.style.maxHeight) || 300;
    const MIN_H = 50, MAX_H = Math.floor(window.innerHeight * 0.8);
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'ns-resize';

    function onMove(ev) {
        ev.preventDefault();
        const delta = ev.clientY - startY;
        const newH = Math.min(MAX_H, Math.max(MIN_H, startMaxHeight + delta));
        content.style.maxHeight = newH + 'px';
    }
    function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

initResultPanelDrag();
