import { api } from '../api.js';
import { ENUMS, can, pct, transferTone } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, fmtTime, kv, meter, table, toast } from '../ui.js';

const state = { direction: '', status: '' };

function yuan(value) {
  return `¥${Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function ledgerRow(item) {
  const ratio = item.usage_ratio === null || item.usage_ratio === undefined ? 0 : item.usage_ratio;
  const over = item.over_budget;
  return `<div class="ledger-row" data-team="${esc(item.team_id)}">
    <div class="alloc-head">
      <span class="mono">${esc(item.code)}</span>
      <b>${esc(item.name)}</b>
      ${over ? '<span class="badge high">超支</span>' : '<span class="badge ok">额度内</span>'}
      <span class="mono num">${yuan(item.remaining_yuan)} 可用</span>
    </div>
    ${meter(ratio, { segments: 32, tone: over ? 'danger' : 'on' })}
    <div class="alloc-meta mono">
      <span>配置预算 ${yuan(item.budget_yuan)}</span>
      <span>借入 ${yuan(item.borrowed_yuan)}</span>
      <span class="lent">转出预占 ${yuan(item.lent_yuan)}</span>
      <span>有效预算 ${yuan(item.effective_budget_yuan)}</span>
      <span>已用 ${yuan(item.used_yuan)}</span>
      <span>剩余额度 ${yuan(item.remaining_yuan)}</span>
    </div>
  </div>`;
}

function openCreate(view, ctx, teams, lenders, selection) {
  const ownTeam = teams.find((team) => String(team.id) === String(ctx.user.team_id));
  const canPickBorrower = ctx.user.role === 'admin';
  const borrowerId = ownTeam ? ownTeam.id : teams[0]?.id;
  const lenderOptions = lenders
    .filter((team) => String(team.team_id) !== String(borrowerId))
    .map((team) => ({
      value: team.team_id,
      label: team.available_yuan === undefined ? team.name : `${team.name} · 可借 ${yuan(team.available_yuan)}`,
    }));
  const projectOptions = [
    '双十一增长看板临时加算',
    '季度经营分析专项',
    '风控模型回溯跑批',
    '供应链补数任务',
  ];
  const { root, close } = drawer({
    title: '申请预算借用',
    subtitle: ownTeam ? `${ownTeam.code} · ${ownTeam.name}` : '管理员可代团队申请',
    body: `<form id="transfer-form" class="transfer-form">
      ${
        canPickBorrower
          ? field({
              label: '借用团队',
              name: 'borrower_team_id',
              value: ownTeam ? ownTeam.id : teams[0]?.id,
              options: teams.map((team) => ({ value: team.id, label: team.name })),
            })
          : `<div class="field"><label>借用团队</label><input value="${esc(ownTeam?.name || '—')}" disabled /></div>
             <input type="hidden" name="borrower_team_id" value="${esc(ownTeam?.id || '')}" />`
      }
      ${field({ label: '转出团队（可留空由管理员指派）', name: 'lender_team_id', options: [{ value: '', label: '由管理员按剩余额度指派' }, ...lenderOptions] })}
      ${field({ label: '借用金额（元）', name: 'amount_yuan', type: 'number', placeholder: '例如 1500', attrs: 'step="0.01" min="0.01"' })}
      ${field({ label: '项目', name: 'project', options: projectOptions.map((value) => ({ value, label: value })) })}
      ${field({ label: '有效期（天）', name: 'valid_days', type: 'number', value: '30', attrs: 'min="1" max="180"' })}
      ${field({ label: '借用原因', name: 'reason', placeholder: '简述用途与归还计划（可选）' })}
      <div class="form-error" id="transfer-error" hidden></div>
      <button class="btn accent" type="submit">提交申请</button>
      <p class="muted mono tiny">提交后进入待审批；管理员按转出团队当前剩余额度审批，超出会被驳回。</p>
    </form>`,
  });
  root.querySelector('#transfer-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const error = root.querySelector('#transfer-error');
    error.hidden = true;
    const payload = {
      borrower_team_id: Number(form.borrower_team_id?.value) || undefined,
      lender_team_id: Number(form.lender_team_id.value) || undefined,
      amount_yuan: Number(form.amount_yuan.value),
      project: form.project.value,
      valid_days: Number(form.valid_days.value) || 30,
      reason: form.reason.value.trim() || null,
    };
    if (!(payload.amount_yuan > 0)) {
      error.textContent = '请填写大于 0 的借用金额';
      error.hidden = false;
      return;
    }
    const submit = form.querySelector('button[type=submit]');
    submit.disabled = true;
    try {
      const created = await api.post('/api/transfers', payload);
      toast(`申请 ${created.transfer_no} 已提交，等待管理员审批`);
      close();
      if (selection) selection.value = '';
      await transfersPage.render(view, ctx);
    } catch (err) {
      error.textContent = errorMessage(err);
      error.hidden = false;
      submit.disabled = false;
    }
  });
}

function openDecision(view, ctx, transfer, lenders) {
  const isAdmin = can(ctx.user.role, 'transfer:approve');
  const isRequester = transfer.requested_by === ctx.user.email;
  const canWithdraw =
    (isRequester || ctx.user.role === 'admin') && ['pending', 'approved'].includes(transfer.status);
  const footer = `
    ${
      isAdmin && transfer.status === 'pending'
        ? `<form id="decision-form" style="display:grid;gap:10px">
            ${field({
              label: '转出团队（按剩余额度指派）',
              name: 'lender_team_id',
              value: transfer.lender_team_id || '',
              options: [
                { value: '', label: '保持申请时的转出团队' },
                ...lenders
                  .filter((team) => String(team.team_id) !== String(transfer.borrower_team_id))
                  .map((team) => ({
                    value: team.team_id,
                    label: team.available_yuan === undefined ? team.name : `${team.name} · 可借 ${yuan(team.available_yuan)}`,
                  })),
              ],
            })}
            ${field({ label: '审批备注', name: 'note', placeholder: '例如：预算充足，按剩余额度放行' })}
            <div class="decision-actions">
              <button class="btn accent" type="submit" name="decision" value="approve">通过借用</button>
              <button class="btn ghost" type="submit" name="decision" value="reject">驳回</button>
            </div>
          </form>`
        : ''
    }
    ${canWithdraw ? '<button class="btn ghost sm" id="withdraw-transfer">撤回申请（释放预占）</button>' : ''}`;
  const { root, close } = drawer({
    title: `${transfer.transfer_no} · ${transfer.project}`,
    subtitle: `${transfer.borrower_team_name || transfer.borrower_team_id} ← ${
      transfer.lender_team_name || '待指派转出团队'
    }`,
    body: `${kv([
      ['状态', `<span class="badge ${transferTone(transfer.status)}">${esc(ENUMS.transferStatus[transfer.status] || transfer.status)}</span>`],
      ['借用团队', esc(transfer.borrower_team_name || '—')],
      ['转出团队', esc(transfer.lender_team_name || '—')],
      ['金额', yuan(transfer.amount_yuan)],
      ['有效期至', fmtTime(transfer.expires_at)],
      ['申请人', `<span class="mono">${esc(transfer.requested_by)}</span>`],
      ['申请时间', fmtTime(transfer.requested_at)],
      ['审批人', esc(transfer.decided_by || '—')],
      ['审批时间', fmtTime(transfer.decided_at)],
      ['审批备注', esc(transfer.decision_note || '—')],
      ['关闭时间', fmtTime(transfer.closed_at)],
      ['关闭原因', esc(transfer.close_reason || '—')],
      ['借用原因', esc(transfer.reason || '—')],
    ])}<p class="muted mono tiny" style="margin-top:10px">只有「已通过且未过期」的借用占用转出团队额度；撤回或过期立即释放。</p>`,
    footer,
  });

  root.querySelector('#decision-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submitter = event.submitter;
    const decision = submitter?.value === 'reject' ? 'reject' : 'approve';
    const note = event.target.note.value.trim() || null;
    const lender = Number(event.target.lender_team_id?.value) || undefined;
    try {
      await api.post(`/api/transfers/${transfer.id}/decision`, { decision, note, lender_team_id: lender });
      toast(decision === 'approve' ? '已通过，额度已预占' : '已驳回');
      close();
      await transfersPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
  root.querySelector('#withdraw-transfer')?.addEventListener('click', async () => {
    try {
      await api.post(`/api/transfers/${transfer.id}/withdraw`, { note: '工作台撤回' });
      toast('已撤回，预占额度已释放');
      close();
      await transfersPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const transfersPage = {
  async render(view, ctx) {
    let teamsData;
    let ledger;
    let transfers;
    let lenders;
    try {
      [teamsData, ledger, transfers] = await Promise.all([
        api.get('/api/teams', { limit: 50 }),
        api.get('/api/transfers/ledger'),
        api.get('/api/transfers', { limit: 50, direction: state.direction, status: state.status }),
      ]);
      lenders = (await api.get('/api/transfers/lenders')).items;
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => transfersPage.render(view, ctx));
      return;
    }
    const canCreate = can(ctx.user.role, 'transfer:create');
    const totals = ledger.totals || {};
    const pendingCount = transfers.items.filter((item) => item.status === 'pending').length;
    const reconciliationRows = Object.values(ledger.reconciliation || {});

    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>预算借用工作台</h2>
          <div class="head-meta">
            <span class="meta">生效调拨 ${ledger.active_transfers} · 待审批 ${ledger.pending_transfers}</span>
            ${canCreate ? '<button class="btn accent" id="create-transfer" data-testid="create-transfer">申请借用</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="alloc-summary">
            <div class="summary-cell"><span class="eyebrow">CONFIG BUDGET</span><b class="mono">${yuan(totals.budget_yuan)}</b></div>
            <div class="summary-cell"><span class="eyebrow">EFFECTIVE</span><b class="mono">${yuan(totals.effective_budget_yuan)}</b></div>
            <div class="summary-cell"><span class="eyebrow">LENT / RESERVED</span><b class="mono">${yuan(totals.lent_yuan)}</b></div>
            <div class="summary-cell"><span class="eyebrow">REMAINING</span><b class="mono">${yuan(totals.remaining_yuan)}</b></div>
          </div>
        </div>
        <div class="panel-body alloc-list" data-testid="ledger-list">
          ${ledger.items.length ? ledger.items.map(ledgerRow).join('') : '<p class="muted mono">还没有团队台账。</p>'}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>借用申请</h2>
          <div class="head-meta">
            <span class="meta">${transfers.total} 条 · 待审批 ${pendingCount}</span>
            ${
              ctx.user.team_id
                ? field({
                    label: '',
                    name: 'direction',
                    value: state.direction,
                    options: [
                      { value: '', label: '全部方向' },
                      { value: 'in', label: '本团队借入' },
                      { value: 'out', label: '本团队转出' },
                    ],
                    attrs: 'data-filter="direction"',
                  })
                : ''
            }
            ${field({
              label: '',
              name: 'status',
              value: state.status,
              options: [
                { value: '', label: '全部状态' },
                ...Object.entries(ENUMS.transferStatus).map(([value, label]) => ({ value, label })),
              ],
              attrs: 'data-filter="status"',
            })}
          </div>
        </div>
        ${table({
          rows: transfers.items,
          empty: { title: '还没有借用申请', hint: '为本团队项目申请额度，管理员按剩余额度审批。' },
          columns: [
            {
              key: 'transfer_no',
              label: '单号 / 项目',
              render: (row) => `<b class="mono">${esc(row.transfer_no)}</b><div class="mono muted">${esc(row.project)}</div>`,
            },
            { key: 'borrower_team_name', label: '借用团队', render: (row) => esc(row.borrower_team_name || '—') },
            {
              key: 'lender_team_name',
              label: '转出团队',
              render: (row) =>
                row.lender_team_name
                  ? esc(row.lender_team_name)
                  : '<span class="mono muted">待指派</span>',
            },
            { key: 'amount_yuan', label: '金额', className: 'num', render: (row) => yuan(row.amount_yuan) },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${transferTone(row.status)}">${esc(ENUMS.transferStatus[row.status] || row.status)}</span>` },
            { key: 'expires_at', label: '有效期至', render: (row) => fmtTime(row.expires_at) },
            { key: 'requested_by', label: '申请人', className: 'mono', render: (row) => esc(row.requested_by) },
            { key: 'decided_by', label: '审批人', className: 'mono', render: (row) => esc(row.decided_by || '—') },
          ],
        })}
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>核算对账</h2>
          <span class="meta">${ledger.run_id ? `cost run #${ledger.run_id} 冻结快照` : '还没有核算'}</span>
        </div>
        <div class="panel-body">
          ${
            reconciliationRows.length
              ? `<div class="recon-list">${reconciliationRows
                  .map((row) => {
                    const team = ledger.items.find((item) => item.team_id === row.team_id);
                    const over = row.status === 'over';
                    return `<div class="recon-line">
                      <div class="recon-team">
                        <span class="mono">${esc(team?.code || `#${row.team_id}`)}</span>
                        <b>${esc(team?.name || '')}</b>
                        <span class="badge ${over ? 'high' : 'ok'}">${esc(ENUMS.reconciliation[row.status] || row.status)}</span>
                      </div>
                      <div class="recon-fig mono">
                        查询成本 ${yuan(row.used_yuan)} / 有效预算 ${yuan(row.effective_budget_yuan)}
                        <span class="tiny">调拨 ${(row.transfer_snapshot || []).length} 笔 · 剩余 ${yuan(row.remaining_yuan)}</span>
                      </div>
                    </div>`;
                  })
                  .join('')}</div>
                  <p class="muted mono tiny" style="margin-top:10px">对账口径在核算时冻结：之后调整预算或撤回调拨，不会改写本次账单。</p>`
              : '<p class="muted mono">发起一次成本核算后，这里展示有效预算与查询成本的对账结果。</p>'
          }
        </div>
      </section>`;

    view.querySelector('#create-transfer')?.addEventListener('click', () => {
      openCreate(view, ctx, teamsData.items, lenders, null);
    });
    view.querySelectorAll('[data-filter]').forEach((node) => {
      node.addEventListener('change', () => {
        state[node.dataset.filter] = node.value;
        transfersPage.render(view, ctx);
      });
    });
    view.querySelectorAll('table.data tbody tr').forEach((node) => {
      node.addEventListener('click', () => {
        const transfer = transfers.items.find((item) => String(item.id) === node.dataset.id);
        if (transfer) openDecision(view, ctx, transfer, lenders);
      });
    });
  },
};

export default transfersPage;
