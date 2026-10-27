/* Account operations, durable inboxes and Multica routing share the existing workspace. */
export function installWorkspace(ctx){
 const {state,api,actions,commit,head,button,e,badge,date,number,avatar,empty,modal,toast,render,navigate,watch,activityItems,drawQR,pollQR,photo,accountPicker,switchAccount}=ctx;
 const controlModes={request_approval:['请求批准','模型生成最终回复，每条正文经你批准后发送。'],auto_approve:['帮我批准','模型自动处理普通交流并发送，特殊事项交给你批准。'],full_access:['完全放开','模型自主决定回复或不回复，直接发送，不等待人工批准。']};
 const labels={stopped:'已停止',waiting:'等待连接',listening:'监听中',error:'连接异常',received:'已接收',queued:'等待助手',generating:'助手处理中',review:'待批准发送',sending:'发送中',sent:'平台已接收',failed:'处理失败',unknown:'结果待核查',ignored:'已忽略',handoff:'需要人工',no_reply:'无需回复',stale:'已有新消息'};
 const pill=(status)=>badge(({listening:'connected',sent:'completed',review:'pending',error:'failed',handoff:'unknown'})[status]||status,labels[status]||status);
 const metric=(title,value,sub='')=>`<div class="stat"><div class="stat-top">${e(title)}</div><strong>${number(value)}</strong><small>${e(sub)}</small></div>`;
 const qs=s=>document.querySelector(s);
 const surface=()=>location.pathname==='/comments'?'comment':'dm';
 const opts=(routes,value,placeholder='仅接收，不调用助手')=>`<option value="">${e(placeholder)}</option>${routes.map(r=>`<option value="${e(r.id)}" ${r.id===value?'selected':''}>${e(r.name)}</option>`).join('')}`;
 let refreshTimer;
 function refreshLater(token,fn){if(token!==state.generation)return;clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>{if(token===state.generation&&!document.querySelector('#dialog').open)fn().catch(()=>{});},4000);}
 async function accounts(token){
  const data=await api('/accounts');
  const active=data.items.filter(a=>a.dm.enabled).length;
  if(!commit(token,head('账号矩阵','独立管理账号连接、私信监听与商品发布。','XIANYU · ACCOUNTS',button('reload','刷新','refresh')+button('matrix-add','绑定账号','plus','primary'))+
   `<div class="stats">${metric('已接入账号',data.items.length,'登录态和业务数据独立保存')}${metric('私信监听',active,'已开启的账号持续同步')}${metric('已导入商品',data.items.reduce((n,a)=>n+a.products,0),'工作台管理范围')}${metric('会话总数',data.items.reduce((n,a)=>n+a.conversations,0),'已同步会话')}</div><div class="matrix-grid">`+
   data.items.map(a=>`<article class="panel matrix-card"><div class="row spread"><div class="account-profile">${avatar(a.name,'account')}<div><h2>${e(a.label||a.name)}</h2><p>${e(a.name)} · ${e(a.id)}</p></div></div>${a.status==='error'?badge('failed',a.check?.needs_verification?'需要安全验证':'需要检查连接'):a.id===state.accountId?badge('connected','当前账号'):badge('draft','已保存')}</div><div class="matrix-facts"><div>账号连接<strong>${a.check?.can_message?'最近检查通过':a.check?.error?'检查未通过':'等待检查'}</strong></div><div>最近检查<strong>${e(date(a.check?.checked_at))}</strong></div><div>私信监听${pill(a.dm.state)}</div><div>商品 / 会话<strong>${a.products} / ${a.conversations}</strong></div></div>${a.check?.error?`<div class="notice warning">${e(a.check.error)}</div>`:''}${a.dm.error?`<div class="error-panel">${e(a.dm.error)}</div>`:''}<footer class="row matrix-actions">${button('matrix-select',a.id===state.accountId?'进入私信监听':'切换账号','','primary small',a.id)}${button('matrix-check','检查连接','refresh','small',a.id)}${a.sms_request?.phase==='verification'?button('sms-browser-open','短信验证','','primary small',a.sms_request.id):''}${button('matrix-reconnect','重新登录','qr','small',a.id)}${!state.meta.demo?button('matrix-browser-session','接回网页登录','','small',a.id):''}${button('matrix-label','备注','edit','small',a.id)}</footer></article>`).join('')+`<article class="panel matrix-add"><div>${empty('接入下一个账号','扫码添加后，各账号可以同时监听。',button('matrix-add','添加闲鱼账号','plus'))}</div></article></div><div class="info-callout">商品、草稿、素材、会话和回复记录按账号保存。切换当前操作账号后，其他已开启的监听继续运行。</div>${state.meta.authenticated?`<p>${button('logout','退出工作台','logout','subtle')}</p>`:''}`))return;
 }
 const requestedSms=new URLSearchParams(location.search).get('sms_request');if(requestedSms)setTimeout(()=>actions['sms-browser-open']({dataset:{id:requestedSms}}).catch(error=>toast(error.message)),700);
 actions['matrix-select']=node=>{if(node.dataset.id===state.accountId)navigate('/dm');else switchAccount(node.dataset.id,'/dm');};
 actions['matrix-check']=async node=>{const token=state.generation,accountId=node.dataset.id;const job=await api('/account/check','POST',{request_id:crypto.randomUUID()},accountId);job.accountId=accountId;try{await watch(job);}finally{if(token===state.generation){state.meta=await api('/meta');await render();}}};
 actions['matrix-label']=async node=>{const data=await api('/accounts');const a=data.items.find(a=>a.id===node.dataset.id);modal('账号备注',`<label>备注名称<input id="matrix-name" maxlength="80" value="${e(a.label||a.name)}"></label>`,button('close','取消')+button('matrix-label-save','保存','','primary',a.id));};
 actions['matrix-label-save']=async node=>{await api('/accounts/'+encodeURIComponent(node.dataset.id),'PATCH',{name:qs('#matrix-name').value});state.meta=await api('/meta');document.querySelector('#dialog').close();await render();};
 actions['matrix-browser-session']=node=>{modal('接回网页登录',`<p>将已登录闲鱼网页的 Cookie 直接提交到工作台，核验此账号的消息连接并恢复已开启的监听。</p><ol><li>在闲鱼网页按 ⌘⌥I，打开 Network（网络），刷新页面。</li><li>选择发往 h5api.m.goofish.com 的请求，在 Headers（标头）→ Request Headers（请求标头）复制 Cookie 的完整值。</li><li>粘贴到下方。登录态只提交给此工作台，请勿发到聊天。</li></ol><label>网页登录态<input id="browser-session-cookie" type="password" autocomplete="off" maxlength="65536" spellcheck="false" placeholder="粘贴 Cookie 值"></label>`,button('close','取消')+button('matrix-browser-session-save','核验并恢复','','primary',node.dataset.id));};
 actions['matrix-browser-session-save']=async node=>{const input=qs('#browser-session-cookie');const cookie=input.value.trim();if(!cookie)throw new Error('请先粘贴网页登录态');input.value='';node.disabled=true;try{const result=await api('/accounts/'+encodeURIComponent(node.dataset.id)+'/browser-session','POST',{cookie},node.dataset.id);document.querySelector('#dialog').close();state.meta=await api('/meta');await render();toast(result.reason);}finally{node.disabled=false;}};
 let smsTimer;
 actions['sms-browser-open']=async node=>{
  const id=node.dataset.id;
  modal('闲鱼短信登录 · 官方验证',`<p id="sms-browser-state" role="status" aria-live="polite">正在连接官方页面…</p><p>按住下方页面的滑块，向右拖动后松开。松开后会将你的操作传给官方页面，再显示结果。发码成功后会自动继续登录。</p><div style="position:relative;line-height:0;max-width:1000px"><img id="sms-browser-frame" draggable="false" alt="可操作的闲鱼官方短信登录页" style="width:100%;touch-action:none;user-select:none;cursor:grab"><svg id="sms-browser-gesture" viewBox="0 0 1000 850" aria-hidden="true" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"><polyline id="sms-browser-trail" fill="none" stroke="var(--ui-accent,#1478ff)" stroke-width="4"></polyline><circle id="sms-browser-pointer" r="10" fill="var(--ui-accent,#1478ff)" opacity=".65" visibility="hidden"></circle></svg></div><label>如官方验证要求文字<input id="sms-browser-text" maxlength="16" autocomplete="off"></label>`,button('sms-browser-type','输入验证文字','','small',id)+button('close','关闭'));
  const image=qs('#sms-browser-frame'),label=qs('#sms-browser-state'),trail=qs('#sms-browser-trail'),pointer=qs('#sms-browser-pointer');
  let points=[],submitting=false,phase='opening';
  const position=event=>{const r=image.getBoundingClientRect();return [Math.max(0,Math.min(1000,(event.clientX-r.left)*1000/r.width)),Math.max(0,Math.min(850,(event.clientY-r.top)*850/r.height))]};
  const feedback=point=>{trail.setAttribute('points',points.map(p=>p.join(',')).join(' '));pointer.setAttribute('cx',point[0]);pointer.setAttribute('cy',point[1]);pointer.setAttribute('visibility','visible')};
  const clearGesture=()=>{points=[];trail.setAttribute('points','');pointer.setAttribute('visibility','hidden');image.style.cursor='grab'};
  image.onpointerdown=event=>{if(phase!=='verification'||submitting||!image.naturalWidth||event.button!==0)return;event.preventDefault();points=[position(event)];image.setPointerCapture(event.pointerId);image.style.cursor='grabbing';label.textContent='正在记录拖动，松开后提交到官方页面';feedback(points[0])};
  image.onpointermove=event=>{if(!points.length)return;event.preventDefault();const point=position(event);if(points.length<99)points.push(point);else points[points.length-1]=point;feedback(point)};
  image.onpointercancel=()=>{clearGesture();label.textContent='拖动已取消，请重新操作'};
  image.onpointerup=async event=>{if(!points.length)return;points.push(position(event));const path=points;points=[];submitting=true;image.style.cursor='wait';label.textContent='正在将你的操作提交到官方页面…';try{await api('/account/sms/'+encodeURIComponent(id)+'/control','POST',{kind:'drag',points:path});label.textContent='操作已提交，正在刷新官方验证结果…';image.src='/api/account/sms/'+encodeURIComponent(id)+'/frame?t='+Date.now()}catch(error){label.textContent=error.message;toast(error.message)}finally{submitting=false;clearGesture()}};
  const refresh=async()=>{if(!document.querySelector('#dialog').open||!image.isConnected)return;if(points.length||submitting){smsTimer=setTimeout(refresh,1000);return}try{const status=await api('/account/sms/'+encodeURIComponent(id));phase=status.phase;label.textContent=status.reason;if(['closed','login_done'].includes(phase))return;if(phase==='verification')image.src='/api/account/sms/'+encodeURIComponent(id)+'/frame?t='+Date.now();}catch(error){label.textContent=error.message;return}smsTimer=setTimeout(refresh,1000)};
  clearTimeout(smsTimer);await refresh();
 };
 actions['sms-browser-type']=async node=>{const input=qs('#sms-browser-text');const text=input.value.trim();input.value='';await api('/account/sms/'+encodeURIComponent(node.dataset.id)+'/control','POST',{kind:'text',text})};
 function qrModal(){modal('扫码接入闲鱼账号','<div id="qr-panel" class="qr-area"></div>',button('close','关闭'));drawQR({status:'idle'});}
 actions['matrix-add']=()=>{qrModal();};
 actions['matrix-reconnect']=async node=>{if(state.meta.demo)throw new Error('演示空间不会发起真实扫码');qrModal();const result=await api('/account/qr','POST',{reconnect_account_id:node.dataset.id});drawQR(result);await pollQR(state.generation);};
 async function inbox(token){
  const s=surface(),title=s==='dm'?'私信监听':'留言监听';
  const data=await api('/monitoring/'+s);
  if(token!==state.generation)return;
  const c=data.config,routes=c.agent?.id?[c.agent]:[],binding='';
  state.inbox=data;state.inboxDirty=false;state.inboxModelsReady=false;
  state.inboxKey=state.accountId+':'+s;state.inboxViews??={};state.inboxViews[state.inboxKey]??={filter:'all',page:1};
  const view=state.inboxViews[state.inboxKey];
  if(!commit(token,head(title,s==='dm'?'接收客户私信，结合聊天记录和商品资料生成回复。':'查看商品留言，跟进助手回复与处理进度。','XIANYU · CHANNEL · MULTICA',button('inbox-refresh','刷新记录','refresh')+(state.meta.demo?button('inbox-demo','模拟新消息','plus'):''))+
   (state.meta.demo?'<div class="notice"><strong>离线演示空间</strong><p>消息、助手回复和发送回执均为模拟数据。</p></div>':'')+
   `<div id="inbox-error" role="alert"></div><div class="listener-layout"><section class="panel"><div class="paneltitle"><h2>监听账号</h2><a data-nav href="/accounts" class="context-link">账号矩阵 →</a></div><div class="panelbody">${accountPicker('监听账号')}<div id="listener-status"></div><p class="hint" id="listener-checked"></p><p class="hint">切换账号后，其他账号已开启的监听继续运行。</p>${!data.supported?'<div class="notice warning">当前账号暂不支持商品留言监听。</div>':s==='comment'&&!data.can_reply?'<div class="notice"><strong>留言回复方式</strong><p>助手生成草稿后，可复制回复并打开商品，在闲鱼 App 发送。</p></div>':''}<details class="listener-details"><summary>查看监听范围</summary><p class="hint">${s==='comment'?'读取当前账号已导入的自有商品；每轮最多 10 件、每件最近 30 条，首次补收近 24 小时留言。':'首次开启从当前时刻接收新私信；恢复监听会补收接口可返回的新消息。聊天记录可在下方查看。'}</p><label>同步间隔（秒）<input id="inbox-interval" type="number" min="15" max="300" step="1" value="${c.interval}"></label></details></div></section>
   <section class="panel"><div class="paneltitle"><h2>回复方式</h2><a data-nav href="/models" class="context-link">模型配置 →</a></div><div class="panelbody"><label>回复 Agent<select id="inbox-route" aria-label="回复 Agent" disabled>${opts(routes,c.agent_id||binding)}</select></label><label class="reply-control">控制方式<select id="inbox-mode" aria-label="控制方式">${Object.entries(controlModes).map(([mode,[label]])=>`<option value="${mode}" ${c.control_mode===mode?'selected':''}>${label}</option>`).join('')}</select></label><p class="hint" id="inbox-mode-help"></p><label class="check"><input id="inbox-send-enabled" type="checkbox" aria-label="回复发送" ${c.auto_reply?'checked':''}>启用自动发送</label><div class="row listener-buttons">${button('inbox-save',c.enabled?'应用回复配置':'开始监听','', 'primary')}${button('inbox-stop','停止监听')}<small class="hint" id="inbox-unsaved" aria-live="polite"></small></div><p class="hint">按已应用的控制方式处理回复；已有更新消息时，旧回复停止发送。</p><div id="inbox-model-status" class="hint" role="status" aria-live="polite">正在加载回复助手列表…</div></div></section></div>
   <div class="stats" id="inbox-stats"></div><section class="panel"><div class="paneltitle"><h2>${s==='dm'?'私信':'留言'}与回复记录</h2><span class="hint">每 5 秒刷新 · 最近 ${data.items.length} 条</span></div><div class="toolbar"><label class="record-filter">处理状态<select id="inbox-filter" aria-label="筛选回复状态"><option value="all">全部状态</option><option value="review">${data.can_reply?'待批准发送':'发送未接通'}</option><option value="sent">平台已接收</option><option value="attention">需要处理</option></select></label><p class="hint">${data.can_reply?'“平台已接收”表示接口确认，尚未确认对方已读。':'复制草稿后，请在闲鱼 App 完成回复。'}</p></div><div id="inbox-records"></div><div id="inbox-pagination" class="pagination"></div></section>`))return;
  qs('#inbox-filter').value=view.filter;
  qs('#inbox-filter').onchange=()=>{view.filter=qs('#inbox-filter').value;view.page=1;drawRecords();};
  const changed=()=>{state.inboxDirty=true;qs('#inbox-unsaved').textContent='有未应用的修改';modeHelp();};
  for(const id of ['inbox-route','inbox-mode','inbox-interval','inbox-send-enabled'])qs('#'+id).onchange=changed;
  qs('#inbox-interval').oninput=changed;
  updateInbox();modeHelp();
  scheduleInbox(token,s,state.accountId);
  void loadInboxModels(token,s,state.accountId);
 }
 async function loadInboxModels(token,s,accountId){
  const request=state.inboxModelRequest=(state.inboxModelRequest||0)+1;
  const active=()=>token===state.generation&&accountId===state.accountId&&request===state.inboxModelRequest;
  const status=qs('#inbox-model-status'),select=qs('#inbox-route'),save=qs('[data-act="inbox-save"]');
  state.inboxModelsReady=false;
  status.textContent='正在加载回复助手列表…';select.disabled=true;save.disabled=true;save.dataset.disabled='true';
  try{
   const models=await api('/models','GET',undefined,accountId);
   if(!active())return;
   if(!models.connected)throw new Error(models.error||'回复助手列表暂时不可用');
   const selected=state.inbox.config.agent_id||models.agent_bindings?.[s]||'';
   select.innerHTML=opts(models.agent_choices?.[s]||[],selected);
   state.inboxModelsReady=true;
   select.disabled=false;save.disabled=!state.inbox.supported;save.dataset.disabled=String(!state.inbox.supported);status.textContent='';
  }catch(error){
   if(active())status.innerHTML=`<div class="notice warning"><p>${e(error.message)}。留言和私信记录仍可查看，已开启的监听可停止。</p>${button('inbox-model-retry','重试加载助手','refresh','small')}</div>`;
  }
 }
 actions['inbox-model-retry']=()=>loadInboxModels(state.generation,surface(),state.accountId);
 function modeHelp(){const mode=qs('#inbox-mode')?.value;qs('#inbox-mode-help').textContent=controlModes[mode]?.[1]||'';}
 function updateInbox(){const d=state.inbox,c=d.config;
  qs('#listener-status').innerHTML=pill(c.state)+badge(c.automatic?'connected':'draft',!c.auto_reply?'自动发送已暂停':controlModes[c.control_mode]?.[0]||'请求批准');
  qs('#listener-checked').textContent='最近检查：'+date(c.last_poll);
  qs('#inbox-error').innerHTML=c.error?`<div class="notice warning"><strong>监听需要检查</strong><p>${e(c.error)}</p><a data-nav href="/accounts">前往账号矩阵恢复连接 →</a></div>`:'';
  qs('#inbox-stats').innerHTML=metric('已接收',d.stats.received,'当前账号累计接收')+metric(d.can_reply?'待批准发送':'发送未接通',d.stats.review,d.can_reply?'批准后提交给原用户':'复制到闲鱼 App 回复')+metric('平台已接收',d.stats.sent,'平台接口已确认')+metric('需要处理',d.stats.attention,'失败、未知或人工处理');
  for(const [action,disabled] of [['inbox-stop',!c.enabled],['inbox-save',!d.supported||!state.inboxModelsReady]]){const node=qs('[data-act="'+action+'"]');node.dataset.disabled=String(disabled);node.disabled=disabled||node.dataset.busy==='true';}
  qs('[data-act="inbox-save"]').textContent=c.enabled?'应用回复配置':'开始监听';
  drawRecords();
 }
 async function refreshInbox(s=surface(),accountId=state.accountId,token=state.generation){const d=await api('/monitoring/'+s,'GET',undefined,accountId);if(token!==state.generation||accountId!==state.accountId)return;state.inbox=d;updateInbox();}
 function scheduleInbox(token,s,accountId){clearTimeout(refreshTimer);refreshTimer=setTimeout(async()=>{if(token!==state.generation)return;try{await refreshInbox(s,accountId,token);}catch(error){if(token===state.generation)qs('#inbox-error').innerHTML=`<div class="notice warning">${e(error.message)}，已保留上次记录，稍后自动重试。</div>`;}finally{if(token===state.generation)scheduleInbox(token,s,accountId);}},5000);}
 function drawRecords(){const target=qs('#inbox-records');if(!target)return;const view=state.inboxViews[state.inboxKey],filter=view.filter;
  const rows=state.inbox.items.filter(r=>filter==='all'||(filter==='attention'?['unknown','failed','handoff'].includes(r.status):r.status===filter));
  const pages=Math.max(1,Math.ceil(rows.length/10));view.page=Math.max(1,Math.min(pages,view.page));const items=rows.slice((view.page-1)*10,view.page*10);
  target.innerHTML=items.length?`<div class="tablewrap"><table class="inbox-table"><thead><tr><th>${surface()==='dm'?'私信用户':'留言用户'}</th><th>收到的${surface()==='dm'?'消息':'留言'}</th><th>模型回复 / 处理说明</th><th>状态与时间</th><th>处理</th></tr></thead><tbody>${items.map(r=>`<tr><td><strong>${e(r.message?.sender_name||r.message?.sender_id||'用户')}</strong>${surface()==='dm'?button('inbox-history','查看聊天','','text-link',r.id):r.message?.url?`<a href="${e(r.message.url)}" target="_blank" rel="noopener noreferrer" class="text-link">查看商品留言 ↗</a>`:''}</td><td><p class="reply-text">${e(r.text||'[图片]')}</p></td><td><p class="reply-text">${e(r.reply||r.error||'等待处理')}</p>${r.reply&&r.error?`<small>${e(r.error)}</small>`:''}<small>Agent：${e(r.execution_agent?.name||r.agent?.name||'未绑定 / 旧记录')}</small>${r.channel_id?`<small title="${e(r.channel_id)}">Channel ${e(r.channel_id.slice(0,12))}</small>`:''}</td><td>${r.status==='review'&&!state.inbox.can_reply?badge('unknown','发送未接通'):pill(r.status)}<small>${e(date(r.created))}</small></td><td><div class="record-actions">${r.status==='review'&&state.inbox.can_reply?button('inbox-approve','批准发送','','text-link',r.id):''}${r.status==='review'&&!state.inbox.can_reply?button('inbox-copy','复制回复','','text-link',r.id):''}${['review','received','handoff','failed'].includes(r.status)?button('inbox-reanalyze','重新分析','','text-link',r.id):''}${['review','received','queued','generating'].includes(r.status)?button('inbox-dismiss','不回复','','text-link',r.id):''}</div></td></tr>`).join('')}</tbody></table></div>`:empty(filter==='all'?'暂无新'+(surface()==='dm'?'私信':'留言'):'没有符合条件的记录',filter==='all'?'开始监听后，新消息和回复进度会显示在这里。':'可以切换状态查看其他记录。');
  qs('#inbox-pagination').innerHTML=`<span class="hint">筛选结果 ${rows.length} 条 · 累计 ${state.inbox.total} 条</span><div class="row">${button('inbox-prev','上一页','','small')}<span class="hint">${view.page} / ${pages}</span>${button('inbox-next','下一页','','small')}</div>`;
  qs('[data-act="inbox-prev"]').disabled=view.page<=1;qs('[data-act="inbox-next"]').disabled=view.page>=pages;
 }
 actions['inbox-prev']=()=>{state.inboxViews[state.inboxKey].page--;drawRecords();};actions['inbox-next']=()=>{state.inboxViews[state.inboxKey].page++;drawRecords();};
 function monitorForm(){return {agent_id:qs('#inbox-route').value,control_mode:qs('#inbox-mode').value,auto_reply:qs('#inbox-send-enabled').checked,interval:Number(qs('#inbox-interval').value)};}
 actions['inbox-save']=async()=>{const body=monitorForm(),s=surface(),id=state.accountId,token=state.generation;if(!Number.isInteger(body.interval)||body.interval<15||body.interval>300)throw new Error('同步间隔应为 15 到 300 秒的整数');body.enabled=true;await api('/monitoring/'+s,'PUT',body,id);if(token!==state.generation)return;state.inboxDirty=false;qs('#inbox-unsaved').textContent='配置已应用';toast('监听配置已应用');await refreshInbox(s,id,token);};
 // Stopping only changes the running state; unsaved reply settings stay in the form.
 actions['inbox-stop']=async()=>{const s=surface(),id=state.accountId,token=state.generation;await api('/monitoring/'+s,'PUT',{enabled:false},id);toast('监听已停止');await refreshInbox(s,id,token);};
 actions['inbox-refresh']=()=>refreshInbox();
 actions['inbox-demo']=async()=>{await api('/monitoring/'+surface()+'/demo-event','POST',{});toast('模拟消息已加入监听');await refreshInbox();};
 function recordContext(node){const row=state.inbox.items.find(r=>r.id===node.dataset.id);if(!row)throw new Error('记录已更新，请刷新后重试');return {row,s:surface(),accountId:state.accountId,token:state.generation};}
 let recordAction;
 actions['inbox-approve']=node=>{recordAction={...recordContext(node),action:'approve'};const {row}=recordAction;modal('批准发送这条回复',`<p class="hint">账号：${e(state.meta.account.name)} · 收件人：${e(row.message?.sender_name||row.message?.sender_id||row.target)}</p><p class="reply-text">${e(row.reply)}</p>`,button('close','取消')+button('inbox-confirm','批准发送','','primary'));};
 actions['inbox-dismiss']=node=>{recordAction={...recordContext(node),action:'dismiss'};modal('不回复这条消息',`<p class="reply-text">${e(recordAction.row.text)}</p><p class="hint">跳过这条记录，后续新消息仍按监听配置处理。</p>`,button('close','取消')+button('inbox-confirm','确认不回复','','primary'));};
 actions['inbox-confirm']=async()=>{const a=recordAction;if(!a||a.accountId!==state.accountId||a.token!==state.generation)throw new Error('账号或页面已变化，请重新查看记录');const result=await api('/monitoring/'+a.s+'/'+a.row.id+'/'+a.action,'POST',{},a.accountId);document.querySelector('#dialog').close();if(a.action==='approve'){result.accountId=a.accountId;await watch(result);}await refreshInbox(a.s,a.accountId,a.token);};
 actions['inbox-reanalyze']=async node=>{const c=recordContext(node);await api('/monitoring/'+c.s+'/'+c.row.id+'/reanalyze','POST',{},c.accountId);toast('已加入重新分析');await refreshInbox(c.s,c.accountId,c.token);};
 actions['inbox-copy']=async node=>{const {row}=recordContext(node);await navigator.clipboard.writeText(row.reply);toast('草稿已复制，请在闲鱼 App 回复');};
 let historyContext,historyVersion=0;
 async function showHistory(context){const version=++historyVersion;historyContext=context;modal('聊天记录','<p class="hint">正在加载聊天记录…</p>');document.querySelector('#dialog').className='drawer';
  const data=await api('/conversations/'+encodeURIComponent(context.row.target)+'/messages','GET',undefined,context.accountId);
  if(version!==historyVersion||context.token!==state.generation||!document.querySelector('#dialog').open)return;
  modal('聊天记录',`<p class="hint">${e(data.conversation.name||context.row.message?.sender_name||'用户')} · 当前已保存 ${data.items.length} 条</p><div class="history-messages">${data.items.map(m=>`<div class="history-message ${m.outgoing?'outgoing':''}"><small>${m.outgoing?'我':'对方'} · ${e(date(m.timestamp))}</small><div class="chat-bubble">${m.kind==='image'?(m.images||[]).map(url=>photo(url,'','聊天图片')).join(''):e(m.text)}</div></div>`).join('')||empty('暂无聊天记录','可读取最新聊天记录。')}</div>`,button('inbox-history-refresh','读取最新记录','refresh')+`<a data-nav class="text-link" href="/messages?cid=${encodeURIComponent(context.row.target)}">进入会话回复 →</a>`+button('close','关闭'));document.querySelector('#dialog').className='drawer';
 }
 actions['inbox-history']=async node=>{try{await showHistory(recordContext(node));}catch(error){document.querySelector('#dialog').close();throw error;}};
 actions['inbox-history-refresh']=async()=>{const c=historyContext;if(c.accountId!==state.accountId||c.token!==state.generation)return;const job=await api('/conversations/'+encodeURIComponent(c.row.target)+'/history','POST',{request_id:crypto.randomUUID(),older:false},c.accountId);job.accountId=c.accountId;await watch(job);if(document.querySelector('#dialog').open&&c.token===state.generation)await showHistory(c);};
 async function models(token){const data=await api('/models');
  if(!commit(token,head('模型配置','按用途选择助手，模型和运行环境由 Multica 统一管理。','MULTICA · AGENTS',button('reload','刷新 Agent','refresh'))+`<section class="panel"><div class="paneltitle"><h2>Multica 统一接入</h2>${badge(data.connected?'connected':'failed',data.connected?(data.demo?'离线演示':'已连接'):'连接异常')}</div><div class="panelbody"><div class="flow"><span>闲鱼工作台</span><b>→</b><span>Channel 路由</span><b>→</b><span>Multica Agent</span><b>→</b><span>结果与回执</span></div><p class="hint">更换底层模型请在 Multica 管理对应助手；更换业务 Agent 请单选实时目录里的 Agent；首次配置默认对应平台与账号同名的 Agent。</p><div class="row"><a href="${e(data.multica_url)}" target="_blank" rel="noopener noreferrer">管理 Multica Agent ↗</a><a href="https://d.58nft.net:9456/" target="_blank" rel="noopener noreferrer">打开 Channel ↗</a></div></div></section>${data.error?`<div class="notice warning">${e(data.error)}</div>`:''}<section class="panel"><div class="paneltitle"><h2>用途与 Agent</h2>${button('models-save','保存 Agent','','primary')}</div><div class="panelbody"><div class="three-col">${[['dm','私信回复','结合客户会话与商品事实起草回复'],['comment','留言回复','处理商品下的留言与交流'],['content','商品文案','为发布草稿起草商品描述']].map(([id,title,desc])=>`<label class="model-choice"><strong>${title}</strong><p class="hint">${desc}</p><select id="model-${id}" aria-label="${title} Agent">${opts(data.agent_choices?.[id]||[],data.agent_bindings?.[id],'不调用 Agent')}</select></label>`).join('')}</div><div class="info-callout">Agent 按账号单独绑定。保存私信或留言 Agent 后，请到对应监听页应用并开启监听。回复会经过状态检查，再提交到对应账号。</div></div></section>`))return;
 }
 actions['models-save']=async()=>{const data={};for(const p of ['dm','comment','content'])data[p]=qs('#model-'+p).value;await api('/models','PUT',{agent_bindings:data});toast('Agent 已保存');};
 async function tasks(token){const data=await api('/jobs');commit(token,head('任务中心','查看同步、发送和发布结果，核对未确认的操作。','OPERATIONS · RECEIPTS',button('reload','刷新','refresh'))+`<section class="panel"><div class="paneltitle"><h2>当前账号 · 最近操作</h2>${badge('connected',state.meta.account.name)}</div><div class="panelbody">${activityItems(data.items)}</div></section>`);if(data.items.some(j=>['running','pending'].includes(j.status)))refreshLater(token,()=>tasks(token));}
 function pause(){clearTimeout(refreshTimer);}
 function resume(page,token){
  if(page==='dm'||page==='comments'){
   const s=surface(),accountId=state.accountId;
   void refreshInbox(s,accountId,token).catch(()=>{});
   scheduleInbox(token,s,accountId);
   if(!state.inboxModelsReady)void loadInboxModels(token,s,accountId);
  }else if(page==='tasks')void tasks(token).catch(()=>{});
 }
 return {accounts,dm:inbox,comments:inbox,models,tasks,pause,resume};
}
