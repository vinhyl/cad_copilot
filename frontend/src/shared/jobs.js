// 跨页面共享的任务进度卡片（FEA / 渲染共用）
// 同一时刻只允许一个活动任务，UI 与轮询状态由单例管理。

export function setupJobCard({ root, getJob, cancelJob, onDone }) {
  const jobTitle   = root.querySelector('#job-title');
  const jobPhase   = root.querySelector('#job-phase');
  const jobBar     = root.querySelector('#job-bar');
  const jobBarFill = root.querySelector('#job-bar-fill');
  const jobDetail  = root.querySelector('#job-detail');
  const jobResult  = root.querySelector('#job-result');
  const cancelBtn  = root.querySelector('#job-cancel');

  const PHASE_LABELS = {
    queued: '排队中', probe: '探测插件', prepare: '生成脚本', cache: '缓存命中',
    interpreter: 'FreeCAD 启动', geometry: '载入几何', faces: '选定约束面',
    setup: 'FEM 设置', mesh: '网格划分', solve: '求解中', post: '读取结果',
    render: '渲染中', done: '完成',
  };

  let activeJob = null;   // { id, kind, timer }

  function render(job) {
    const { progress = {}, status } = job;
    const pct = progress.percent;
    jobPhase.textContent = `${PHASE_LABELS[progress.phase] || progress.phase || status}`
      + (typeof pct === 'number' ? ` · ${Math.round(pct)}%` : '');
    if (typeof pct === 'number') {
      jobBar.classList.remove('indeterminate');
      jobBarFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    } else {
      jobBar.classList.add('indeterminate');
      jobBarFill.style.width = '';
    }
    jobDetail.textContent = progress.detail || '';
    cancelBtn.style.display = (status === 'queued' || status === 'running') ? '' : 'none';
  }

  function stopPolling() {
    if (activeJob?.timer) clearInterval(activeJob.timer);
    activeJob = null;
  }

  function showError(job) {
    jobResult.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'jr-err';
    el.textContent = job.status === 'cancelled'
      ? '已取消（几何与缓存不受影响）' : `失败：${job.error || '未知错误'}`;
    jobResult.appendChild(el);
  }

  // 结果视图按 kind 交给调用方渲染（跨页复用：FEA/渲染结构不同）
  function track(started, kind, label, { renderResultFn, reportFn }) {
    if (activeJob) {
      reportFn?.('已有任务在进行（见右下角卡片，可取消后再发起新任务）', true);
      return;
    }
    activeJob = { id: started.job_id, kind, timer: null };
    jobTitle.textContent = label;
    jobResult.innerHTML = '';
    jobDetail.textContent = '';
    jobPhase.textContent = '排队中';
    jobBar.classList.add('indeterminate');
    root.classList.remove('hidden');
    reportFn?.(`${label}已提交（任务 ${started.job_id.slice(0, 8)}）`);

    const poll = async () => {
      let job;
      try {
        job = await getJob(activeJob.id);
      } catch {
        return;
      }
      render(job);
      if (job.status === 'done') {
        stopPolling();
        renderResultFn?.(job.result);
        reportFn?.(`${label}完成`);
        onDone?.({ kind, result: job.result });
      } else if (job.status === 'error' || job.status === 'cancelled') {
        stopPolling();
        showError(job);
        reportFn?.(`${label}${job.status === 'cancelled' ? '已取消' : '失败'}`, true);
      }
    };
    activeJob.timer = setInterval(poll, 800);
    poll();
  }

  cancelBtn.addEventListener('click', async () => {
    if (!activeJob) return;
    cancelBtn.disabled = true;
    try {
      await cancelJob(activeJob.id);
      jobPhase.textContent = '正在取消…';
    } finally {
      cancelBtn.disabled = false;
    }
  });

  function hide() { root.classList.add('hidden'); }
  function destroy() { stopPolling(); hide(); }

  return { track, destroy, get isActive() { return !!activeJob; } };
}
