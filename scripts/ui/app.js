(() => {
  const byId = (id) => document.getElementById(id);

  const elements = {
    runId: byId('runId'),
    phase: byId('phase'),
    taskBrief: byId('taskBrief'),
    plannerSummary: byId('plannerSummary'),
    plannerSelect: byId('plannerSelect'),
    plannerId: byId('plannerId'),
    plannerStatus: byId('plannerStatus'),
    plannerSummaryText: byId('plannerSummaryText'),
    plannerErrors: byId('plannerErrors'),
    judgeStatus: byId('judgeStatus'),
    judgeSummary: byId('judgeSummary'),
    judgeErrors: byId('judgeErrors'),
    finalPlanStatus: byId('finalPlanStatus'),
    finalPlanEditor: byId('finalPlanEditor'),
    finalPlanPreview: byId('finalPlanPreview'),
    previewPane: byId('previewPane'),
    editToggle: byId('editToggle'),
    previewToggle: byId('previewToggle'),
    resetLatest: byId('resetLatest'),
    acceptBtn: byId('acceptBtn'),
    saveBtn: byId('saveBtn'),
    saveStatus: byId('saveStatus'),
    refineContext: byId('refineContext'),
    refineBtn: byId('refineBtn'),
    refineStatus: byId('refineStatus'),
    connectionStatus: byId('connectionStatus'),
    lastUpdated: byId('lastUpdated'),
    sessionCountdown: byId('sessionCountdown'),
    keepOpenToggle: byId('keepOpenToggle'),
    keepOpenStatus: byId('keepOpenStatus'),
    refreshModelsBtn: byId('refreshModelsBtn'),
    modelsStatus: byId('modelsStatus'),
    agentListSelect: byId('agentListSelect'),
    setJudgeBtn: byId('setJudgeBtn'),
    toggleAgentBtn: byId('toggleAgentBtn'),
    removeAgentBtn: byId('removeAgentBtn'),
    agentListStatus: byId('agentListStatus'),
    newAgentName: byId('newAgentName'),
    newAgentKind: byId('newAgentKind'),
    newAgentModel: byId('newAgentModel'),
    addAgentBtn: byId('addAgentBtn'),
    addAgentStatus: byId('addAgentStatus'),
    modelCatalogText: byId('modelCatalogText'),
    agentControlStatus: byId('agentControlStatus')
  };

  const token = new URLSearchParams(window.location.search).get('token');
  const stateEndpoint = token ? `/api/state?token=${encodeURIComponent(token)}` : '/api/state';
  const eventsEndpoint = token ? `/events?token=${encodeURIComponent(token)}` : '/events';

  let currentState = {
    run_id: '',
    task_brief: '',
    phase: '',
    planners: [],
    judge: { status: '', summary: '', errors: [] },
    final_plan: '',
    errors: [],
    timestamps: {}
  };

  let latestFinalPlan = '';
  let editorDirty = false;
  let editorLocked = false;
  let previewVisible = true;
  let selectedPlannerId = '';
  let sessionDeadline = null;
  let sessionKeepOpen = false;
  let sessionTimer = null;
  let selectedConfigAgentName = '';

  const setText = (el, value, fallback = '—') => {
    if (!el) {
      return;
    }
    const text = typeof value === 'string' && value.trim() ? value : fallback;
    el.textContent = text;
  };

  const setStatus = (el, message, tone) => {
    if (!el) {
      return;
    }
    el.textContent = message;
    if (tone) {
      el.dataset.tone = tone;
    } else {
      delete el.dataset.tone;
    }
  };

  const attemptCloseUi = () => {
    try {
      window.close();
    } catch (error) {
      // ignore
    }
    setTimeout(() => {
      if (!window.closed) {
        setStatus(elements.saveStatus, 'accepted — you can close this tab');
      }
    }, 300);
  };

  const formatTimestamp = (value) => {
    if (!value) {
      return '—';
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleTimeString();
  };

  const updateLastUpdated = (value) => {
    setText(elements.lastUpdated, `last update: ${formatTimestamp(value)}`);
  };

  const formatRemaining = (ms) => {
    const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const pad = (value) => String(value).padStart(2, '0');
    if (hours > 0) {
      return `${hours}:${pad(minutes)}:${pad(seconds)}`;
    }
    return `${minutes}:${pad(seconds)}`;
  };

  const updateSessionCountdown = () => {
    if (!elements.sessionCountdown) {
      return;
    }
    if (sessionKeepOpen) {
      setText(elements.sessionCountdown, 'session: keep open');
      return;
    }
    if (!sessionDeadline) {
      setText(elements.sessionCountdown, 'session: —');
      return;
    }
    const remaining = sessionDeadline - Date.now();
    if (remaining <= 0) {
      setText(elements.sessionCountdown, 'session: closing…');
      return;
    }
    setText(elements.sessionCountdown, `session: ${formatRemaining(remaining)} left`);
  };

  const setSessionState = (state) => {
    sessionKeepOpen = Boolean(state?.keep_open);
    sessionDeadline = state?.ui_deadline ? new Date(state.ui_deadline).getTime() : null;
    if (elements.keepOpenToggle) {
      elements.keepOpenToggle.checked = sessionKeepOpen;
    }
    if (!sessionTimer) {
      sessionTimer = window.setInterval(updateSessionCountdown, 1000);
    }
    updateSessionCountdown();
  };

  const setStatusLink = (el, message, url) => {
    if (!el) {
      return;
    }
    el.textContent = '';
    const text = message || '';
    if (text) {
      el.appendChild(document.createTextNode(text + ' '));
    }
    if (url) {
      const link = document.createElement('a');
      link.href = url;
      link.textContent = 'Open new run';
      link.target = '_blank';
      link.rel = 'noopener';
      el.appendChild(link);
    }
  };

  const updatePlannerList = (planners) => {
    if (!elements.plannerSelect) {
      return;
    }
    elements.plannerSelect.textContent = '';
    if (!Array.isArray(planners) || planners.length === 0) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'Waiting for planner output…';
      elements.plannerSelect.appendChild(option);
      updatePlannerDetail(null);
      return;
    }

    planners.forEach((planner) => {
      const option = document.createElement('option');
      option.value = planner.id || '';
      option.textContent = planner.id || 'planner';
      elements.plannerSelect.appendChild(option);
    });

    if (!selectedPlannerId || !planners.some((p) => p.id === selectedPlannerId)) {
      selectedPlannerId = planners[0].id || '';
    }
    elements.plannerSelect.value = selectedPlannerId;
    const active = planners.find((planner) => planner.id === selectedPlannerId) || planners[0];
    updatePlannerDetail(active);
  };

  const updatePlannerDetail = (planner) => {
    if (!planner) {
      setText(elements.plannerId, '—');
      setText(elements.plannerStatus, 'pending');
      setText(elements.plannerSummaryText, '—');
      if (elements.plannerErrors) {
        elements.plannerErrors.textContent = '';
      }
      return;
    }
    setText(elements.plannerId, planner.id || 'planner');
    setText(elements.plannerStatus, planner.status || 'pending');
    setText(elements.plannerSummaryText, planner.summary || '—');
    if (elements.plannerErrors) {
      elements.plannerErrors.textContent = '';
      if (Array.isArray(planner.errors) && planner.errors.length > 0) {
        planner.errors.forEach((err) => {
          const item = document.createElement('li');
          item.textContent = err;
          elements.plannerErrors.appendChild(item);
        });
      }
    }
  };

  const updateJudge = (judge) => {
    setText(elements.judgeStatus, judge?.status || 'pending');
    setText(elements.judgeSummary, judge?.summary || '—');
    elements.judgeErrors.textContent = '';

    if (Array.isArray(judge?.errors) && judge.errors.length > 0) {
      judge.errors.forEach((err) => {
        const item = document.createElement('li');
        item.textContent = err;
        elements.judgeErrors.appendChild(item);
      });
    }
  };

  const updateFinalPlan = (planText) => {
    latestFinalPlan = planText || '';
    if (!editorDirty) {
      elements.finalPlanEditor.value = latestFinalPlan;
    }
    elements.finalPlanPreview.textContent = latestFinalPlan || '—';
    const status = editorDirty ? 'edited locally' : 'synced';
    setText(elements.finalPlanStatus, status);
  };

  const applyState = (state) => {
    currentState = state;
    setText(elements.runId, `run: ${state.run_id || '—'}`);
    setText(elements.phase, `phase: ${state.phase || '—'}`);
    setText(elements.taskBrief, state.task_brief || '—');

    const planners = Array.isArray(state.planners) ? state.planners : [];
    elements.plannerSummary.textContent = `${planners.length} planners`;
    updatePlannerList(planners);

    updateJudge(state.judge || { status: 'pending', summary: '', errors: [] });
    updateFinalPlan(state.final_plan || '');

    updateLastUpdated(state.timestamps?.updated_at || new Date().toISOString());
    setSessionState(state);
    updateAgentControls(state);
  };

  const updateAgentControls = (state) => {
    const agents = Array.isArray(state?.config_agents) ? state.config_agents : [];
    const judgeName = state?.config_judge || '';
    if (elements.agentListSelect) {
      elements.agentListSelect.textContent = '';
      if (agents.length === 0) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No configured agents';
        elements.agentListSelect.appendChild(option);
      } else {
        agents.forEach((agent) => {
          const option = document.createElement('option');
          option.value = agent.name || '';
          const model = agent.model ? ` (${agent.model})` : '';
          const enabledMark = agent.enabled ? '' : ' [disabled]';
          const judgeMark = judgeName && judgeName === agent.name ? ' [judge]' : '';
          option.textContent = `${agent.name || 'agent'}${model}${enabledMark}${judgeMark}`;
          elements.agentListSelect.appendChild(option);
        });
      }
      if (!selectedConfigAgentName || !agents.some((a) => a.name === selectedConfigAgentName)) {
        selectedConfigAgentName = agents[0]?.name || '';
      }
      elements.agentListSelect.value = selectedConfigAgentName;
    }

    if (elements.modelCatalogText) {
      const catalog = state?.model_catalog;
      const items = Array.isArray(catalog?.items) ? catalog.items : [];
      if (items.length === 0) {
        elements.modelCatalogText.textContent = 'No model data yet. Click Refresh Models.';
      } else {
        const lines = [];
        for (const item of items) {
          const available = Array.isArray(item.available_models) ? item.available_models.join(', ') : '';
          const source = item.source || 'fallback';
          const warning = item.warning ? ` | warning: ${item.warning}` : '';
          lines.push(`${item.agent}: ${available} | source: ${source}${warning}`);
        }
        elements.modelCatalogText.textContent = lines.join('\n');
      }
    }

    if (elements.agentControlStatus) {
      const count = agents.length;
      elements.agentControlStatus.textContent = `${count} configured`;
    }
  };

  const fetchState = async () => {
    try {
      const response = await fetch(stateEndpoint, { cache: 'no-store' });
      if (!response.ok) {
        throw new Error('failed');
      }
      const payload = await response.json();
      applyState(payload);
    } catch (error) {
      setStatus(elements.connectionStatus, 'state fetch failed');
    }
  };

  const handleEvent = (message) => {
    if (!message || typeof message !== 'object') {
      return;
    }

    if (message.type === 'phase_change') {
      currentState.phase = message.payload?.phase || currentState.phase;
      applyState(currentState);
      updateLastUpdated(message.payload?.timestamp);
      return;
    }

    if (message.type === 'planner_update') {
      const planner = message.payload?.planner;
      if (planner && planner.id) {
        const planners = Array.isArray(currentState.planners) ? [...currentState.planners] : [];
        const index = planners.findIndex((item) => item.id === planner.id);
        if (index >= 0) {
          planners[index] = planner;
        } else {
          planners.push(planner);
        }
        currentState.planners = planners;
      }
      applyState(currentState);
      updateLastUpdated(message.payload?.timestamp);
      return;
    }

    if (message.type === 'judge_update') {
      if (message.payload?.judge) {
        currentState.judge = message.payload.judge;
      }
      applyState(currentState);
      updateLastUpdated(message.payload?.timestamp);
      return;
    }

    if (message.type === 'final_plan') {
      currentState.final_plan = message.payload?.final_plan || currentState.final_plan;
      applyState(currentState);
      updateLastUpdated(message.payload?.timestamp);
      return;
    }

    if (message.type === 'session_update') {
      currentState.keep_open = message.payload?.keep_open;
      currentState.ui_deadline = message.payload?.ui_deadline;
      setSessionState(currentState);
      updateLastUpdated(message.payload?.timestamp);
      return;
    }

    if (message.type === 'action_result') {
      const action = message.payload?.action;
      const status = message.payload?.message || message.payload?.status || 'updated';
      if (action === 'save') {
        setStatus(elements.saveStatus, status);
      } else if (action === 'accept') {
        setStatus(elements.saveStatus, status);
        attemptCloseUi();
      } else if (action === 'refine') {
        setStatus(elements.refineStatus, status);
      } else if (action === 'keepalive') {
        setStatus(elements.keepOpenStatus, status);
      } else if (action === 'models-refresh') {
        setStatus(elements.modelsStatus, status);
      } else if (action === 'agent-add') {
        setStatus(elements.addAgentStatus, status);
        fetchState();
      } else if (action === 'agent-remove' || action === 'agent-toggle' || action === 'judge-set') {
        setStatus(elements.agentListStatus, status);
        fetchState();
      }
      updateLastUpdated(message.payload?.timestamp);
      return;
    }
  };

  const connectEvents = () => {
    if (!window.EventSource) {
      setStatus(elements.connectionStatus, 'SSE not supported');
      return;
    }

    const source = new EventSource(eventsEndpoint);
    setStatus(elements.connectionStatus, 'connecting…');

    source.onopen = () => {
      setStatus(elements.connectionStatus, 'connected');
      fetchState();
    };

    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleEvent(data);
      } catch (error) {
        setStatus(elements.connectionStatus, 'event parse error');
      }
    };

    source.onerror = () => {
      setStatus(elements.connectionStatus, 'reconnecting…');
    };
  };

  const postAction = async (path, payload, statusEl) => {
    if (!token) {
      setStatus(statusEl, 'missing token', 'warning');
      return;
    }
    const suppressQueued = path === '/api/save';
    setStatus(statusEl, suppressQueued ? 'saving…' : 'sending…');
    try {
      const response = await fetch(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-UI-Token': token
        },
        body: JSON.stringify(payload || {})
      });
      if (!response.ok) {
        throw new Error('action failed');
      }
      if (!suppressQueued) {
        setStatus(statusEl, 'queued');
      }
    } catch (error) {
      setStatus(statusEl, 'failed');
    }
  };

  if (elements.finalPlanEditor) {
    elements.finalPlanEditor.addEventListener('input', () => {
      if (editorLocked) {
        return;
      }
      editorDirty = elements.finalPlanEditor.value !== latestFinalPlan;
      elements.finalPlanPreview.textContent = elements.finalPlanEditor.value || '—';
      const status = editorDirty ? 'edited locally' : 'synced';
      setText(elements.finalPlanStatus, status);
    });
  }

  elements.acceptBtn.addEventListener('click', () => {
    postAction('/api/accept', { final_plan: elements.finalPlanEditor.value }, elements.saveStatus);
  });

  elements.saveBtn.addEventListener('click', () => {
    postAction('/api/save', { final_plan: elements.finalPlanEditor.value }, elements.saveStatus);
  });

  if (elements.refineBtn) {
    elements.refineBtn.addEventListener('click', () => {
      postAction(
        '/api/refine',
        {
          context: elements.refineContext.value,
          final_plan: elements.finalPlanEditor.value
        },
        elements.refineStatus
      );
    });
  }

  if (elements.plannerSelect) {
    elements.plannerSelect.addEventListener('change', () => {
      selectedPlannerId = elements.plannerSelect.value;
      const planners = Array.isArray(currentState.planners) ? currentState.planners : [];
      const active = planners.find((planner) => planner.id === selectedPlannerId) || planners[0];
      updatePlannerDetail(active || null);
    });
  }

  if (elements.keepOpenToggle) {
    elements.keepOpenToggle.addEventListener('change', () => {
      postAction(
        '/api/keepalive',
        { keep_open: elements.keepOpenToggle.checked },
        elements.keepOpenStatus
      );
    });
  }

  if (elements.agentListSelect) {
    elements.agentListSelect.addEventListener('change', () => {
      selectedConfigAgentName = elements.agentListSelect.value;
    });
  }

  if (elements.refreshModelsBtn) {
    elements.refreshModelsBtn.addEventListener('click', () => {
      postAction('/api/models-refresh', {}, elements.modelsStatus);
    });
  }

  if (elements.addAgentBtn) {
    elements.addAgentBtn.addEventListener('click', () => {
      const name = (elements.newAgentName?.value || '').trim();
      const kind = elements.newAgentKind?.value || 'custom';
      const model = (elements.newAgentModel?.value || '').trim();
      if (!name) {
        setStatus(elements.addAgentStatus, 'name required');
        return;
      }
      const payload = {
        agent: {
          name,
          kind,
          model,
          auth_mode: 'login',
          enabled: true
        }
      };
      postAction('/api/agent-add', payload, elements.addAgentStatus);
    });
  }

  if (elements.removeAgentBtn) {
    elements.removeAgentBtn.addEventListener('click', () => {
      const name = selectedConfigAgentName || elements.agentListSelect?.value || '';
      if (!name) {
        setStatus(elements.agentListStatus, 'select agent');
        return;
      }
      postAction('/api/agent-remove', { name }, elements.agentListStatus);
    });
  }

  if (elements.setJudgeBtn) {
    elements.setJudgeBtn.addEventListener('click', () => {
      const name = selectedConfigAgentName || elements.agentListSelect?.value || '';
      if (!name) {
        setStatus(elements.agentListStatus, 'select agent');
        return;
      }
      postAction('/api/judge-set', { name }, elements.agentListStatus);
    });
  }

  if (elements.toggleAgentBtn) {
    elements.toggleAgentBtn.addEventListener('click', () => {
      const name = selectedConfigAgentName || elements.agentListSelect?.value || '';
      if (!name) {
        setStatus(elements.agentListStatus, 'select agent');
        return;
      }
      const agents = Array.isArray(currentState?.config_agents) ? currentState.config_agents : [];
      const target = agents.find((agent) => agent.name === name);
      const nextEnabled = !(target?.enabled ?? true);
      postAction('/api/agent-toggle', { name, enabled: nextEnabled }, elements.agentListStatus);
    });
  }

  fetchState();
  connectEvents();
})();
