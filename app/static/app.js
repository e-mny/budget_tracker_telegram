'use strict';
const $ = id => document.getElementById(id);
const cash = cents => new Intl.NumberFormat('en-SG', {style:'currency',currency:'SGD',currencyDisplay:'narrowSymbol'}).format(cents / 100);
const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const monthLabel = month => new Date(month + '-01T12:00:00').toLocaleDateString('en-SG', {month:'long',year:'numeric'});
const dateLabel = date => new Date(date + 'T12:00:00').toLocaleDateString('en-SG', {day:'2-digit',month:'short'});
let receiptDraft=null, csvDraft=null, editingGoal=null, contributingGoal=null, contributionKey=null;
let data, chartMode = 'daily', editingExpense = null, editingSubscription = null, refreshSequence = 0, toastTimer;

async function api(path, method='GET', body) {
  const response = await fetch('/api' + path, {method, credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-Penny-Client':'dashboard'},
    ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const detail = typeof result.detail === 'string' ? result.detail : 'Check the entered values and try again.';
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return result;
}
function showLogin() {
  $('app-view').hidden = true;
  $('login-view').hidden = false;
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  $('password').focus();
}
function toast(message) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => {$('toast').hidden = true;}, 4000);
}
async function refresh() {
  const sequence = ++refreshSequence;
  $('refresh-state').textContent = 'Updating…';
  $('page-error').hidden = true;
  const month = $('month').value;
  try {
    const result = await api('/summary' + (month ? '?month=' + encodeURIComponent(month) : ''));
    if (sequence !== refreshSequence) return;
    data = result;
    $('login-view').hidden = true;
    $('app-view').hidden = false;
    $('loading').hidden = true;
    $('dashboard').hidden = false;
    $('month').value = data.month;
    $('month').max = data.today.slice(0,7);
    $('expense-date').max = data.today;
    render();
    $('refresh-state').textContent = 'Up to date';
  } catch(error) {
    if (sequence !== refreshSequence) return;
    $('refresh-state').textContent = 'Could not update';
    $('loading').hidden = true;
    $('page-error').textContent = error.message;
    $('page-error').hidden = false;
    if (!data) {
      showLogin();
      $('login-error').textContent = error.status === 401 ? '' : error.message;
    }
  }
}
function render() {
  $('total').textContent = cash(data.total);
  $('total-sub').textContent = `${data.count} recorded expense${data.count === 1 ? '' : 's'} this month`;
  $('budget').textContent = data.budget === null ? 'Not set yet' : cash(data.budget);
  $('budget').classList.toggle('unset', data.budget === null);
  $('set-budget-stat').textContent = data.budget === null ? 'Set your monthly limit' : 'Edit monthly budget';
  $('remaining').textContent = data.remaining === null ? '—' : cash(Math.abs(data.remaining));
  $('remaining').classList.toggle('negative', data.remaining !== null && data.remaining < 0);
  $('remaining-sub').textContent = data.remaining === null ? 'Set a budget to see what’s left' : data.remaining < 0 ? 'Over your monthly budget' : `${Math.round(Math.max(0, data.remaining) / data.budget * 100)}% of your budget still available`;
  $('average').textContent = cash(data.daily_average);
  $('average-sub').textContent = `Across ${data.elapsed_days} calendar days`;
  const previousFilter = $('filter-category').value;
  const options = data.categories.map(cat => `<option value="${cat.id}">${escapeHTML(cat.label)}</option>`).join('');
  $('filter-category').innerHTML = '<option value="">All categories</option>' + options;
  $('filter-category').value = previousFilter;
  $('expense-category').innerHTML = options;
  $('data-note').textContent = `SGD · ${data.timezone} · Recorded expenses only`;
  renderCategories(); renderTrend(); renderBudget(); renderSubscriptions(); renderTransactions(); renderFeatures();
}
function renderCategories() {
  const categories = data.categories.filter(cat => cat.amount > 0).sort((a,b) => b.amount - a.amount);
  let offset = 0;
  const circles = categories.map(cat => {
    const size = cat.amount / data.total * 100;
    const circle = `<circle cx="100" cy="100" r="77" fill="none" stroke="${cat.color}" stroke-width="24" pathLength="100" stroke-dasharray="${size} ${100-size}" stroke-dashoffset="${-offset}"><title>${escapeHTML(cat.label)}: ${cash(cat.amount)} (${Math.round(size)}%)</title></circle>`;
    offset += size;
    return circle;
  }).join('');
  const totalDisplay = data.total >= 10000000 ? '$' + (data.total / 100000).toFixed(1) + 'k' : cash(data.total);
  $('category-chart').innerHTML = `<div class="donut"><svg viewBox="0 0 200 200" role="img" aria-label="Spending by category"><circle cx="100" cy="100" r="77" fill="none" stroke="#f0eef8" stroke-width="24"/>${circles}</svg><div class="donut-center"><strong title="${cash(data.total)}">${totalDisplay}</strong><span>Total this month</span></div></div><div class="category-legend">${categories.length ? categories.map(cat => `<div class="legend-row"><i class="legend-dot" style="background:${cat.color}"></i><span class="legend-label" title="${escapeHTML(cat.label)}">${escapeHTML(cat.label)}</span><span class="legend-amount">${cash(cat.amount)}<span class="legend-percent">${Math.round(cat.amount / data.total * 100)}%</span></span></div>`).join('') : '<p class="empty-state">A fresh start.<br>Add an expense to see your breakdown.</p>'}</div>`;
}
function renderTrend() {
  const width=640, height=267, left=53, right=20, top=22, bottom=37;
  const plotWidth=width-left-right, plotHeight=height-top-bottom;
  let total = 0;
  const values = data.daily.map(d => {total += d.amount; return chartMode === 'daily' ? d.amount : total;});
  const maxValue = Math.max(1000, ...values);
  const scale = 10 ** Math.floor(Math.log10(maxValue));
  const ceiling = Math.ceil(maxValue / scale) * scale;
  const y = value => top + plotHeight - value / ceiling * plotHeight;
  const x = day => left + (day-0.5) / data.days_in_month * plotWidth;
  let svg = '';
  for (let tick=0; tick<=4; tick++) {
    const value=ceiling*tick/4, pos=y(value);
    const label=value >= 100000 ? '$' + (value/100000).toFixed(value%100000 ? 1:0) + 'k' : '$' + Math.round(value/100);
    svg += `<line x1="${left}" x2="${width-right}" y1="${pos}" y2="${pos}" stroke="#edf0f6" stroke-dasharray="4 5"/><text x="${left-10}" y="${pos+4}" text-anchor="end" fill="#9ba3b2" font-size="12">${label}</text>`;
  }
  const isCurrent = data.month === data.today.slice(0,7);
  const elapsed = isCurrent ? data.elapsed_days : data.days_in_month;
  if (chartMode === 'daily') {
    for (let day=1; day<=elapsed; day++) {
      const value=values[day-1];
      const barWidth=Math.max(3, plotWidth/data.days_in_month-7);
      if (value > 0) svg += `<rect x="${x(day)-barWidth/2}" y="${y(value)}" width="${barWidth}" height="${Math.max(1, value/ceiling*plotHeight)}" rx="3" fill="${day===data.elapsed_days && isCurrent ? '#7963ed' : '#c2b8f6'}"><title>${day} ${monthLabel(data.month)}: ${cash(value)}</title></rect>`;
    }
  } else {
    const points=values.slice(0,elapsed).map((v,i) => `${x(i+1)},${y(v)}`);
    svg += `<defs><linearGradient id="area-fill" x1="0" x2="0" y1="0" y2="1"><stop stop-color="#7963ed" stop-opacity=".18"/><stop offset="1" stop-color="#7963ed" stop-opacity="0"/></linearGradient></defs><polygon points="${x(1)},${y(0)} ${points.join(' ')} ${x(elapsed)},${y(0)}" fill="url(#area-fill)"/><polyline points="${points.join(' ')}" fill="none" stroke="#7963ed" stroke-width="2.5" stroke-linejoin="round"/>`;
    values.slice(0,elapsed).forEach((value,i) => {svg += `<circle cx="${x(i+1)}" cy="${y(value)}" r="4" fill="#7963ed"><title>Through day ${i+1}: ${cash(value)}</title></circle>`;});
  }
  [1,5,10,15,20,25,data.days_in_month].forEach(day => {svg += `<text x="${x(day)}" y="${height-11}" text-anchor="middle" fill="#9ba3b2" font-size="12">${day}</text>`;});
  const dailyDescription = data.daily.slice(0, elapsed).map(d => `${d.day}: ${cash(d.amount)}`).join('; ');
  $('trend-chart').innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${chartMode==='daily'?'Daily':'Cumulative'} spending for ${monthLabel(data.month)}"><desc>${escapeHTML(dailyDescription)}</desc>${svg}</svg>`;
  $('chart-context').textContent = monthLabel(data.month);
}
function renderBudget() {
  $('edit-budget').textContent = data.budget === null ? 'Set budget' : 'Edit budget';
  if (data.budget === null) {
    $('budget-body').innerHTML = '<div class="budget-content budget-unset"><p>Give your spending a direction. Set a monthly limit to see what’s available as you go.</p><button class="button primary" id="empty-budget-button">Set a monthly budget</button></div>';
    $('empty-budget-button').addEventListener('click', openBudget);
    return;
  }
  const percent=data.total/data.budget*100;
  const over=data.remaining<0;
  const isCurrent=data.month===data.today.slice(0,7);
  const daysLeft=isCurrent ? data.days_in_month - data.elapsed_days : 0;
  let insight;
  if (over) insight = `You’re ${cash(-data.remaining)} over your budget for ${monthLabel(data.month)}.`;
  else if (isCurrent && daysLeft > 0) insight = `${cash(data.remaining)} left for the month. That’s about ${cash(Math.floor(data.remaining/daysLeft))} per day across the ${daysLeft} remaining days.`;
  else insight = `You finished ${monthLabel(data.month)} with ${cash(data.remaining)} of your budget remaining.`;
  $('budget-body').innerHTML = `<div class="budget-content"><div class="budget-main"><strong>${cash(data.total)}</strong><span>of ${cash(data.budget)} budget</span></div><div class="progress-track" role="progressbar" aria-label="Monthly budget spent" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(100,Math.round(percent))}" aria-valuetext="${Math.round(percent)} percent of budget spent"><div class="progress-fill ${over?'over':''}" style="width:${Math.min(100,percent)}%"></div></div><div class="budget-caption"><span>${Math.round(percent)}% spent</span><span>${cash(Math.abs(data.remaining))} ${over?'over budget':'remaining'}</span></div><div class="budget-insight ${over?'warn':''}"><b aria-hidden="true">${over?'!':'✦'}</b><span>${insight}</span></div></div>`;
}
function billingLabel(sub) {
  const singular = {days:'day',weeks:'week',months:'month',years:'year'}[sub.interval_unit];
  return sub.interval_count === 1 ? `Every ${singular}` : `Every ${sub.interval_count} ${sub.interval_unit}`;
}
function renderSubscriptions() {
  $('subscription-list').innerHTML = data.subscriptions.map(sub => {
    const due = data.upcoming_subscriptions.find(item=>item.id===sub.id)?.next_due;
    const label = sub.payment_type === 'investment' ? 'Investment' : data.categories.find(cat=>cat.id===sub.category)?.label;
    return `<button class="subscription-row ${sub.active?'':'inactive'}" data-subscription="${sub.id}" aria-label="Edit recurring payment ${escapeHTML(sub.name)}"><span class="sub-icon">${escapeHTML(sub.name.slice(0,1).toUpperCase())}</span><span class="sub-copy"><strong>${escapeHTML(sub.name)}</strong><small>${escapeHTML(label)} · ${sub.active ? escapeHTML(billingLabel(sub)) + (due?' · Next '+dateLabel(due):'') : 'Paused'}</small></span><span class="sub-amount">${cash(sub.amount)}</span></button>`;
  }).join('');
  $('subscription-total').textContent = cash(data.subscription_total) + ' / month';
  $('annual-commitment').textContent = `Estimated annual commitment: ${cash(data.annual_subscription_total)}. Next: ${data.upcoming_subscriptions.filter(s=>s.next_due).sort((a,b)=>a.next_due.localeCompare(b.next_due)).slice(0,1).map(s=>s.name+' · '+dateLabel(s.next_due)).join('') || 'No active renewals'}. Day/week estimates use a 365-day year.`;
  $('subscription-list').querySelectorAll('[data-subscription]').forEach(button => button.addEventListener('click', () => openSubscription(Number(button.dataset.subscription))));
}
function renderTransactions() {
  if (!data) return;
  const query=$('search').value.trim().toLowerCase(), filter=$('filter-category').value;
  const rows=data.expenses.filter(row => (!filter || row.category===filter) && (!query || `${row.note} ${row.category} ${row.spent_on} ${data.categories.find(c=>c.id===row.category).label}`.toLowerCase().includes(query)));
  $('transaction-count').textContent = `${rows.length} of ${data.count} expense${data.count===1?'':'s'} · ${monthLabel(data.month)}`;
  $('transaction-rows').innerHTML = rows.map(row => {
    const cat=data.categories.find(c=>c.id===row.category);
    const source=({'telegram':'Telegram','subscription':'Auto-recorded','dashboard':'Dashboard','csv':'CSV import','receipt':'Receipt'})[row.source] || 'Dashboard';
    return `<tr><td>${dateLabel(row.spent_on)}</td><td title="${escapeHTML(row.note)}">${escapeHTML(row.note || cat.label)}</td><td><span class="category-pill" style="--pill-bg:${cat.color}16;--pill-color:${cat.color}"><i class="legend-dot" style="background:${cat.color}"></i>${escapeHTML(cat.label)}</span></td><td><span class="source-tag">${source}</span></td><td class="amount-cell">${cash(row.amount)}${row.original_currency?`<span class="original-value">${escapeHTML(row.original_currency)} ${escapeHTML(row.original_amount)}</span>`:""}</td><td><button class="edit-expense" data-expense="${row.id}" aria-label="Edit ${escapeHTML(row.note || cat.label)}">✎</button></td></tr>`;
  }).join('');
  $('table-empty').hidden = rows.length>0;
  $('table-empty').textContent = data.count ? 'No expenses match your filters.' : 'Nothing recorded yet. Add your first expense here or send /add on Telegram.';
  $('transaction-rows').querySelectorAll('[data-expense]').forEach(button => button.addEventListener('click', () => openExpense(Number(button.dataset.expense))));
}
function openExpense(identifier=null) {
  if (!data) return;
  editingExpense=identifier;
  receiptDraft=null;
  $('receipt-review').hidden=true;
  const row=data.expenses.find(row=>row.id===identifier);
  $('expense-form').reset();
  $('expense-title').textContent = row ? 'Edit expense' : 'Add an expense';
  $('expense-amount').value = row ? (row.amount/100).toFixed(2) : '';
  $('expense-category').value = row?.category || 'food';
  $('expense-date').value = row?.spent_on || data.today;
  $('expense-note').value = row?.note || '';
  $('expense-currency').innerHTML = '<option value="">None · SGD only</option>' + data.currencies.map(code=>`<option value="${code}">${code}</option>`).join('');
  $('expense-currency').value=row?.original_currency || '';
  $('expense-original').value=row?.original_amount || '';
  $('currency-details').open=Boolean(row?.original_currency);
  $('delete-expense').hidden = !row;
  $('expense-error').textContent = '';
  $('expense-dialog').showModal();
}
function openBudget() {
  if (!data) return;
  $('budget-dialog-description').textContent = `Your limit for ${monthLabel(data.month)}.`;
  $('budget-amount').value = data.budget === null ? '' : (data.budget/100).toFixed(2);
  $('budget-error').textContent = '';
  $('budget-dialog').showModal();
}
function openSubscription(identifier=null) {
  if (!data) return;
  editingSubscription=identifier;
  const sub=data.subscriptions.find(sub=>sub.id===identifier);
  $('subscription-title').textContent=sub?'Edit recurring payment':'Add recurring payment';
  $('subscription-name').value=sub?.name || '';
  $('subscription-amount').value=sub?(sub.amount/100).toFixed(2):'';
  $('subscription-category').innerHTML=data.categories.map(cat=>`<option value="${cat.id}">${escapeHTML(cat.label)}</option>`).join('');
  $('subscription-category').value=sub?.category || 'home';
  $('subscription-type').value=sub?.payment_type || 'expense';
  $('subscription-type').disabled=Boolean(sub);
  updatePaymentType();
  $('subscription-interval').value=sub?.interval_count || 1;
  $('subscription-unit').value=sub?.interval_unit || 'months';
  $('subscription-start').value=sub?.start_date || data.today;
  $('subscription-active').checked=sub?Boolean(sub.active):true;
  $('subscription-error').textContent='';
  $('subscription-dialog').showModal();
}
function updatePaymentType() {
  const investment = $('subscription-type').value === 'investment';
  $('subscription-category-field').hidden=investment;
  $('subscription-category').disabled=investment;
}
$('subscription-type').addEventListener('change',updatePaymentType);
async function saveForm(event, errorID, action) {
  event.preventDefault();
  const button=event.currentTarget.querySelector('button[type="submit"]');
  button.disabled=true;
  $(errorID).textContent='';
  try {await action();} catch(error) {$(errorID).textContent=error.message;} finally {button.disabled=false;}
}
$('login-form').addEventListener('submit', event => saveForm(event,'login-error',async()=>{
  await api('/login','POST',{password:$('password').value});
  $('password').value='';
  await refresh();
}));
$('logout').addEventListener('click',async()=>{try{await api('/logout','POST');showLogin();data=null;}catch(error){toast(error.message);}});
$('refresh').addEventListener('click',refresh);
$('month').addEventListener('change',()=>{if($('month').value) refresh();});
$('add-expense').addEventListener('click',()=>openExpense());
$('set-budget-stat').addEventListener('click',openBudget);
$('edit-budget').addEventListener('click',openBudget);
$('new-subscription').addEventListener('click',()=>openSubscription());
$('search').addEventListener('input',renderTransactions);
$('filter-category').addEventListener('change',renderTransactions);
document.querySelectorAll('[data-chart]').forEach(button=>button.addEventListener('click',()=>{
  chartMode=button.dataset.chart;
  document.querySelectorAll('[data-chart]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});
  if(data) renderTrend();
}));
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
document.querySelectorAll('[data-nav]').forEach(link=>link.addEventListener('click',()=>{
  document.querySelectorAll('[data-nav]').forEach(l=>l.classList.toggle('active',l===link));
}));
$('expense-form').addEventListener('submit',event=>saveForm(event,'expense-error',async()=>{
  const payload={amount:$('expense-amount').value,category:$('expense-category').value,spent_on:$('expense-date').value,note:$('expense-note').value,original_currency:$('expense-currency').value || null,original_amount:$('expense-currency').value ? $('expense-original').value : null};
  await api(receiptDraft?'/receipts/'+receiptDraft+'/confirm':editingExpense?'/expenses/'+editingExpense:'/expenses',editingExpense?'PUT':'POST',payload);
  receiptDraft=null;
  $('expense-dialog').close();
  await refresh();
  toast('Expense saved');
}));
$('budget-form').addEventListener('submit',event=>saveForm(event,'budget-error',async()=>{
  await api('/budgets/'+data.month,'PUT',{amount:$('budget-amount').value});
  $('budget-dialog').close();await refresh();toast('Monthly budget saved');
}));
$('subscription-form').addEventListener('submit',event=>saveForm(event,'subscription-error',async()=>{
  const payload={name:$('subscription-name').value,amount:$('subscription-amount').value,payment_type:$('subscription-type').value,category:$('subscription-type').value==='investment'?'other':$('subscription-category').value,interval_count:Number($('subscription-interval').value),interval_unit:$('subscription-unit').value,start_date:$('subscription-start').value,active:$('subscription-active').checked};
  await api(editingSubscription?'/subscriptions/'+editingSubscription:'/subscriptions',editingSubscription?'PUT':'POST',payload);
  $('subscription-dialog').close();await refresh();toast('Recurring payment saved');
}));
$('delete-expense').addEventListener('click',()=>{
  const row=data.expenses.find(row=>row.id===editingExpense);
  $('confirm-description').textContent=`${row.note || data.categories.find(c=>c.id===row.category).label} · ${cash(row.amount)} · ${row.spent_on}`;
  $('confirm-error').textContent='';
  $('confirm-dialog').showModal();
});
$('cancel-delete').addEventListener('click',()=>$('confirm-dialog').close());
$('confirm-delete').addEventListener('click',async()=>{
  $('confirm-delete').disabled=true;
  try {
    await api('/expenses/'+editingExpense,'DELETE');
    $('confirm-dialog').close();$('expense-dialog').close();await refresh();toast('Expense deleted');
  }catch(error){$('confirm-error').textContent=error.message;}finally{$('confirm-delete').disabled=false;}
});
// Refresh when returning from Telegram, without disturbing an open edit or a search.
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden && data && !document.querySelector('dialog[open]')) refresh();
});
function renderFeatures() {
  renderComparison(); renderLimits(); renderGoals(); renderInvestments();
  const settings=data.notification_settings;
  $('alerts-body').innerHTML=`<div class="notification-line"><span class="notification-icon">◎</span><div><strong>Budget alerts ${settings.budget_alerts?'on':'off'}</strong><p>${settings.thresholds.join('%, ')}% of monthly and category limits. Sent once per threshold each month.</p></div></div><div class="notification-line"><span class="notification-icon">↻</span><div><strong>Renewal reminders ${settings.renewal_reminders?'on':'off'}</strong><p>${settings.reminder_days} day${settings.reminder_days===1?'':'s'} before configured billing dates, after 09:00. Telegram worker required.</p></div></div>`;
}
function renderComparison() {
  const compare=data.comparison;
  const suffix=compare.partial?`Through day ${data.elapsed_days} vs ${dateLabel(compare.previous_through)}`:'Full calendar months';
  $('comparison-context').textContent=`${monthLabel(data.month)} vs ${monthLabel(compare.previous_month)} · ${suffix}`;
  if(!compare.has_history) {
    $('comparison-body').innerHTML='<p class="empty-inline">Your spending story starts here. Add earlier records or come back next month for a comparison.</p>';
    return;
  }
  const change=compare.change===0?'Same recorded spending as the previous period':`${cash(Math.abs(compare.change))} ${compare.change>0?'more':'less'}${compare.change_percent===null?'':` (${Math.abs(compare.change_percent)}%)`} than the previous period`;
  const rows=compare.categories.filter(c=>c.current||c.previous).sort((a,b)=>(b.current+b.previous)-(a.current+a.previous));
  const ceiling=Math.max(1,...rows.flatMap(c=>[c.current,c.previous]));
  $('comparison-body').innerHTML=`<div class="comparison-totals"><div><span>${monthLabel(data.month)}</span><strong>${cash(compare.current_total)}</strong></div><div><span>${monthLabel(compare.previous_month)}</span><strong>${cash(compare.previous_total)}</strong></div></div><p class="comparison-change ${compare.change>0?'up':''}">${change}</p>${rows.map(c=>`<div class="compare-row"><span>${escapeHTML(c.label)}</span><div class="compare-bars"><div class="compare-bar" style="width:${c.current/ceiling*100}%;background:${c.color}" title="This period: ${cash(c.current)}"></div><div class="compare-bar previous" style="width:${c.previous/ceiling*100}%" title="Previous period: ${cash(c.previous)}"></div></div><span class="compare-value">${cash(c.current)}<br><span class="muted">${cash(c.previous)}</span></span></div>`).join('')}<div class="compare-legend"><span><i class="legend-dot" style="background:#7963ed"></i>Selected period</span><span><i class="legend-dot" style="background:#dce1ec"></i>Previous period</span></div>`;
}
function renderLimits() {
  const categories=data.categories.filter(c=>data.category_budgets[c.id]);
  $('category-limits-body').innerHTML=categories.length?categories.map(cat=>{
    const limit=data.category_budgets[cat.id];
    const percent=Math.round(cat.amount/limit*100);
    return `<div class="category-limit-row"><div class="limit-heading"><strong>${escapeHTML(cat.label)}</strong><span>${cash(cat.amount)} / ${cash(limit)}</span></div><div class="progress-track"><div class="progress-fill ${percent>100?'over':''}" style="width:${Math.min(100,percent)}%;background:${percent>100?'#d56877':cat.color}"></div></div><span class="stat-sub">${percent}% spent · ${cash(Math.abs(limit-cat.amount))} ${percent>100?'over limit':'remaining'}</span></div>`;
  }).join(''):'<p class="empty-inline">Set individual limits for food, shopping, travel, or any other category. You choose where you want a little more structure.</p>';
}
function renderGoals() {
  $('goals-list').innerHTML=data.goals.length?data.goals.map(goal=>{
    const percent=Math.round(goal.saved/goal.target*100), complete=goal.saved>=goal.target;
    return `<article class="goal-card"><div class="goal-top"><h3>${escapeHTML(goal.name)}</h3><button class="icon-button" data-goal-edit="${goal.id}" aria-label="Edit goal ${escapeHTML(goal.name)}">✎</button></div><div class="goal-total">${cash(goal.saved)}</div><span class="goal-target">of ${cash(goal.target)} target</span><div class="progress-track"><div class="progress-fill" style="width:${Math.min(100,percent)}%;${complete?'background:#75ad78':''}"></div></div><div class="goal-meta"><span class="${complete?'goal-complete':''}">${complete?'Goal reached':percent+'% saved'}</span><span>${goal.target_date?'Target '+dateLabel(goal.target_date)+' '+goal.target_date.slice(0,4):'Your own pace'}</span></div><button class="text-button" data-goal-add="${goal.id}">＋ Add contribution</button></article>`;
  }).join(''):'<p class="empty-state">What are you making room for?<br>Add a savings goal to watch it grow.</p>';
  document.querySelectorAll('[data-goal-edit]').forEach(button=>button.addEventListener('click',()=>openGoal(Number(button.dataset.goalEdit))));
  document.querySelectorAll('[data-goal-add]').forEach(button=>button.addEventListener('click',()=>{
    contributingGoal=Number(button.dataset.goalAdd); contributionKey=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
    $('contribution-goal-name').textContent=data.goals.find(g=>g.id===contributingGoal).name;
    $('contribution-amount').value='';$('contribution-error').textContent='';$('contribution-dialog').showModal();
  }));
}
function renderInvestments() {
  $('investments-body').innerHTML=`<div class="comparison-totals"><div><span>Invested in ${monthLabel(data.month)}</span><strong>${cash(data.investment_total)}</strong></div><div><span>Monthly commitment</span><strong>${cash(data.monthly_investment_commitment)}</strong></div></div><p class="empty-inline">Investment contributions are separate from spending and your expense budget.</p>${data.investment_contributions.map(row=>`<div class="investment-row"><span>${escapeHTML(row.note)}<small>${dateLabel(row.invested_on)}</small></span><strong>${cash(row.amount)}</strong></div>`).join('') || '<p class="empty-inline">No investment contributions recorded this month.</p>'}`;
}
function openGoal(identifier=null) {
  editingGoal=identifier;
  const goal=data.goals.find(g=>g.id===identifier);
  $('goal-title').textContent=goal?'Edit savings goal':'Add savings goal';
  $('goal-name').value=goal?.name||'';
  $('goal-target').value=goal?(goal.target/100).toFixed(2):'';
  $('goal-saved').value=goal?(goal.saved/100).toFixed(2):'0';
  $('goal-date').value=goal?.target_date||'';
  $('delete-goal').hidden=!goal;$('goal-error').textContent='';$('goal-dialog').showModal();
}
$('manage-category-limits').addEventListener('click',()=>{
  $('limit-category').innerHTML=data.categories.map(c=>`<option value="${c.id}">${escapeHTML(c.label)}</option>`).join('');
  $('category-month').textContent='Limits effective from '+monthLabel(data.month)+'.';
  $('category-error').textContent=''; loadLimit(); $('category-dialog').showModal();
});
function loadLimit() {const amount=data.category_budgets[$('limit-category').value];$('limit-amount').value=amount?(amount/100).toFixed(2):'';}
$('limit-category').addEventListener('change',loadLimit);
$('category-form').addEventListener('submit',event=>saveForm(event,'category-error',async()=>{
  await api('/category-budgets/'+data.month+'/'+$('limit-category').value,'PUT',{amount:$('limit-amount').value||null});
  $('category-dialog').close();await refresh();toast('Category limit saved');
}));
$('add-goal').addEventListener('click',()=>openGoal());
$('goal-form').addEventListener('submit',event=>saveForm(event,'goal-error',async()=>{
  await api(editingGoal?'/goals/'+editingGoal:'/goals',editingGoal?'PUT':'POST',{name:$('goal-name').value,target:$('goal-target').value,saved:$('goal-saved').value,target_date:$('goal-date').value||null});
  $('goal-dialog').close();await refresh();toast('Savings goal saved');
}));
$('contribution-form').addEventListener('submit',event=>saveForm(event,'contribution-error',async()=>{
  await api('/goals/'+contributingGoal+'/contributions','POST',{amount:$('contribution-amount').value,key:contributionKey});
  $('contribution-dialog').close();await refresh();toast('Contribution saved');
}));
$('delete-goal').addEventListener('click',async()=>{
  if(!window.confirm('Delete this savings goal and its contribution records? This does not change your expenses.')) return;
  try{await api('/goals/'+editingGoal,'DELETE');$('goal-dialog').close();await refresh();toast('Goal deleted');}catch(error){$('goal-error').textContent=error.message;}
});
$('edit-alerts').addEventListener('click',()=>{
  const settings=data.notification_settings;
  $('alerts-enabled').checked=settings.budget_alerts;$('alert-thresholds').value=settings.thresholds.join(', ');
  $('reminders-enabled').checked=settings.renewal_reminders;$('reminder-days').value=settings.reminder_days;
  $('alerts-error').textContent='';$('alerts-dialog').showModal();
});
$('alerts-form').addEventListener('submit',event=>saveForm(event,'alerts-error',async()=>{
  const parts=$('alert-thresholds').value.split(',').map(s=>s.trim());
  if(parts.some(s=>!/^\d+$/.test(s))) throw new Error('Enter comma-separated whole-number thresholds, for example 50, 80, 100.');
  await api('/notification-settings','PUT',{budget_alerts:$('alerts-enabled').checked,thresholds:parts.map(Number),renewal_reminders:$('reminders-enabled').checked,reminder_days:Number($('reminder-days').value)});
  $('alerts-dialog').close();await refresh();toast('Notification settings saved');
}));
async function uploadFile(path,file) {
  const body=new FormData();body.append('file',file);
  const response=await fetch('/api'+path,{method:'POST',credentials:'same-origin',headers:{'X-Penny-Client':'dashboard'},body});
  const result=await response.json();
  if(!response.ok){if(response.status===401) showLogin();throw new Error(typeof result.detail==='string'?result.detail:'Could not process this file.');}
  return result;
}
$('upload-receipt').addEventListener('click',()=>$('receipt-file').click());
$('receipt-file').addEventListener('change',async()=>{
  const file=$('receipt-file').files[0];if(!file)return;
  $('upload-receipt').disabled=true;$('tools-status').textContent='Reading receipt locally…';
  try{
    const result=await uploadFile('/receipts/preview',file);openExpense();receiptDraft=result.id;
    const payload=result.payload;
    $('expense-title').textContent='Review receipt';$('expense-amount').value=payload.amount;
    $('expense-category').value=payload.category;$('expense-note').value=payload.note;$('expense-date').value=payload.spent_on;
    $('expense-currency').value=payload.original_currency||'';$('expense-original').value=payload.original_amount||'';
    $('currency-details').open=Boolean(payload.original_currency);$('receipt-review').hidden=false;
    $('receipt-warning').textContent=payload.warning;$('receipt-text').textContent=payload.ocr_text;
    $('tools-status').textContent='Receipt extracted. Review the suggested fields before saving.';
  }catch(error){$('tools-status').textContent=error.message;}finally{$('upload-receipt').disabled=false;$('receipt-file').value='';}
});
$('cancel-receipt').addEventListener('click',async()=>{
  try{if(receiptDraft) await api('/drafts/'+receiptDraft+'/cancel','POST');receiptDraft=null;$('expense-dialog').close();toast('Receipt draft discarded');}catch(error){$('expense-error').textContent=error.message;}
});
$('upload-csv').addEventListener('click',()=>$('csv-file').click());
$('csv-file').addEventListener('change',async()=>{
  const file=$('csv-file').files[0];if(!file)return;
  $('upload-csv').disabled=true;$('tools-status').textContent='Preparing import preview…';
  try{
    const result=await uploadFile('/csv/preview',file);csvDraft=result.id;
    $('csv-summary').textContent=`${result.total_rows} rows · ${result.new_rows} new · ${result.total_rows-result.new_rows} matching rows will be skipped. No records have been imported yet.`;
    $('csv-rows').innerHTML=result.rows.slice(0,100).map(row=>`<tr><td>${escapeHTML(row.payload.spent_on)}</td><td>${escapeHTML(row.payload.note)}</td><td>${escapeHTML(row.payload.category)}</td><td>${escapeHTML(row.payload.amount)}</td><td>${row.duplicate?'Skip matching':'New record'}</td></tr>`).join('');
    $('csv-error').textContent='';$('confirm-csv').disabled=result.new_rows===0;$('csv-dialog').showModal();
    $('tools-status').textContent='CSV ready for review.';
  }catch(error){$('tools-status').textContent=error.message;}finally{$('upload-csv').disabled=false;$('csv-file').value='';}
});
$('confirm-csv').addEventListener('click',async()=>{
  $('confirm-csv').disabled=true;
  try{const result=await api('/csv/'+csvDraft+'/confirm','POST');$('csv-dialog').close();csvDraft=null;await refresh();toast(`${result.imported} imported · ${result.skipped} skipped`);}catch(error){$('csv-error').textContent=error.message;}finally{$('confirm-csv').disabled=false;}
});
$('cancel-csv').addEventListener('click',async()=>{
  try{if(csvDraft) await api('/drafts/'+csvDraft+'/cancel','POST');csvDraft=null;$('csv-dialog').close();}catch(error){$('csv-error').textContent=error.message;}
});
async function downloadCSV(all=false) {
  try{
    const response=await fetch('/api/csv/export'+(all?'':'?month='+encodeURIComponent(data.month)),{credentials:'same-origin'});
    if(!response.ok)throw new Error('Could not export expenses. Sign in again if your session expired.');
    const link=document.createElement('a'),url=URL.createObjectURL(await response.blob());link.href=url;link.download=`penny-expenses-${all?'all':data.month}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){$('tools-status').textContent=error.message;}
}
$('export-month').addEventListener('click',()=>downloadCSV());$('export-all').addEventListener('click',()=>downloadCSV(true));
refresh();
