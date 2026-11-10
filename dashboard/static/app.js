'use strict';
// Preserve the camera's local wall time; EXIF without offset is not UTC.
function captureDate(row){return row.captured_at?String(row.captured_at).replace('T',' '):row.file_created_at?date(row.file_created_at)+'（创建时间）':'未记录';}
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const numberFormatter=new Intl.NumberFormat('zh-CN');
const dateFormatter=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
const shortDateFormatter=new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const num = value => numberFormatter.format(Number(value||0));
const bytes = value => {let n=Number(value||0), i=0;const units=['B','KB','MB','GB','TB'];while(n>=1024&&i<4){n/=1024;i++;}return `${n.toFixed(i&&n<10?1:0)} ${units[i]}`;};
const date = (value, short=false) => {if(!value)return '未记录';const d=typeof value==='number'?new Date(value*1000):new Date(value);if(Number.isNaN(d.getTime()))return '未记录';return (short?shortDateFormatter:dateFormatter).format(d);};
const elapsed = value => {let n=Math.max(0,Math.floor(Number(value||0)));if(n<60)return `${n} 秒`;if(n<3600)return `${Math.floor(n/60)} 分 ${n%60} 秒`;return `${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分`;};
const ago = value => !value?'尚未检查':`${elapsed(Date.now()/1000-value)}前`;
const badge = (label,kind='neutral') => `<span class="badge ${kind}"><i></i>${esc(label)}</span>`;
const empty = (message='暂无记录') => `<div class="empty-state"><div class="empty-symbol">◫</div>${esc(message)}</div>`;
const statusLabel={pending:'待映射',linked:'已映射',deleted:'已移除',intent:'执行意图',error:'执行失败',unknown:'待核对'};
const originLabel={manual:'手动清理',syncthing:'Syncthing',unknown:'来源未知'};
const backupLabel={unknown:'未确认',confirmed:'逐张确认',aggregate_confirmed:'整体确认',failed:'备份失败',not_confirmed:'未确认'};
const reasonLabel={relaxed_no_free_space:'相册无可释放内容 · 补充清理',photos_retained:'已备份照片残留清理',manual_after_backup:'单文件备份确认后清理',unresolved_manual_intent:'未完成删除意图 · 待核对'};
const phaseOrder=['mapping','sync','backup','release','release_uncertain','deletions','observe','cleanup','final_sync','complete'];
const phaseLabels={mapping:'建立硬链接',sync:'等待同步',backup:'等待相册备份',release:'相册释放',release_uncertain:'释放操作及结果确认',deletions:'等待删除同步',observe:'残留观察',cleanup:'手动补充清理',final_sync:'最终删除同步',complete:'已完成'};
const state={view:'overview',data:null,pages:{batches:1,files:1,audits:1},batchFilter:'',busy:false,drawerOpener:null,requestVersion:0,drawerVersion:0,apiCache:new Map(),apiInflight:new Map(),listController:null,listPending:null,listView:null,overviewPending:null,runtimePending:null,refreshPending:{},listSignatures:{}};
async function api(url,{signal,cacheMs=3000}={}){
 if(signal?.aborted)throw signal.reason;
 const saved=state.apiCache.get(url);
 if(saved&&Date.now()-saved.time<cacheMs)return saved.data;
 const current=state.apiInflight.get(url);
 if(current&&!current.controller.signal.aborted)return current.promise;
 const controller=new AbortController(),forward=()=>controller.abort(signal.reason);
 signal?.addEventListener('abort',forward,{once:true});
 const timer=setTimeout(()=>controller.abort(new DOMException('请求超时','TimeoutError')),15000);
 const entry={controller,promise:null};
 entry.promise=(async()=>{
  try{
   const response=await fetch(url,{signal:controller.signal});
   let data;try{data=await response.json();}catch{throw new Error('服务器返回了无法识别的响应');}
   if(!response.ok)throw new Error(data.error||'监控接口暂时不可用');
   if(cacheMs>0){state.apiCache.set(url,{data,time:Date.now()});while(state.apiCache.size>64)state.apiCache.delete(state.apiCache.keys().next().value);}
   return data;
  }finally{
   clearTimeout(timer);signal?.removeEventListener('abort',forward);
   if(state.apiInflight.get(url)===entry)state.apiInflight.delete(url);
  }
 })();
 state.apiInflight.set(url,entry);
 return entry.promise;
}
function runtimeNotice(run){
 const ctrl=run.controller||{};
 const message=!run.available?'控制器尚未发布运行状态，文件库存与批次记录仍可查询。':run.controller_stale?'控制器状态已超过 3 分钟未更新，以下设备状态是最近一次检查结果。':ctrl.state==='error'?(ctrl.error||'控制器检测到异常，已暂停相关操作。'):'';
 banner(message);
}

function banner(message){$('error-banner').hidden=!message;$('error-banner').textContent=message||'';}
function syncProgress(){
 let box=$('sync-progress');if(!box||box.hidden)return;
 const run=state.latestRuntime||state.data?.runtime||{},sync=run.syncthing||{};
 const raw=sync.completion;
 box.classList.toggle('unavailable',Boolean(sync.error)||sync.connected!==true);
 if(raw==null||!Number.isFinite(Number(raw))){box.innerHTML='<div class="subtle">'+(sync.error?'Syncthing 异常，暂无法读取进度。':'等待 Syncthing 进度数据…')+'</div>';return;}
 const value=Math.max(0,Math.min(100,Number(raw))),percent=(Math.floor(value*10)/10).toFixed(1);
 const label=sync.error?'Syncthing 异常，以下为最近成功进度':sync.connected!==true?'Pixel 未连接，以下为最近检查结果':sync.ready?'两端已同步完成':value>=100?'数据已传完，等待扫描或删除同步确认':'正在同步到 Pixel';
 const remaining=sync.need_remote_bytes==null?'—':bytes(sync.need_remote_bytes);
 const total=sync.total_bytes==null?'—':bytes(sync.total_bytes);
 const items=sync.need_remote==null?'—':num(sync.need_remote);
 const deletes=sync.need_remote_deletes==null?'—':num(sync.need_remote_deletes);
 box.innerHTML=`<div class="sync-progress-top"><span>${esc(label)}</span><strong>${percent}%</strong></div><div class="sync-progress-track" role="progressbar" aria-label="Pixel 文件夹同步进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${value}"><div style="width:${value}%"></div></div><div class="sync-progress-details"><span>剩余 ${remaining} / ${total}</span><span>${items} 项待同步 · ${deletes} 项待删除</span></div><div class="sync-progress-foot">文件夹整体进度 · 检查于 ${esc(date(sync.progress_checked_at||sync.checked_at,true))}</div>`;
}
function updateSyncProgress(run){
 state.latestRuntime=run;
 const ctrl=run.controller||{},active=state.data?.active;
 const phase=ctrl.phase||(active?.phase),id=ctrl.batch_id||active?.id;
 let box=$('sync-progress');
 if(!box){box=document.createElement('div');box.id='sync-progress';box.className='sync-progress';$('active-batch').appendChild(box);}
 box.hidden=!['sync','deletions','final_sync'].includes(phase)||!id;
 syncProgress();
}
function detailItem(label,value){return `<div class="detail-item"><small>${esc(label)}</small><span>${esc(value??'未记录')}</span></div>`;}
function roundLink(row){return `<button class="batch-link" data-batch="${esc(row.id)}">#${esc(row.id.slice(0,8))} ↗</button>`;}
function phaseBadge(row){return badge(row.phase_label||phaseLabels[row.phase]||row.phase,row.phase==='complete'?'green':'blue');}
function batchTable(items,compact=false){if(!items.length)return empty('新的备份批次将在这里出现');return `<table><thead><tr><th>批次</th><th>开始时间</th><th>文件数</th><th>状态</th>${compact?'':'<th>备份确认</th><th>手动删除</th><th>总耗时</th>'}</tr></thead><tbody>${items.map(r=>`<tr><td>${roundLink(r)}</td><td>${esc(date(r.started_at,true))}</td><td>${num(r.file_count)}</td><td>${phaseBadge(r)}</td>${compact?'':`<td>${r.backed_up==null?'未记录':num(r.backed_up)}</td><td>${r.summary?num(r.summary.manual_deleted):'—'}</td><td>${r.started_at?esc(elapsed((r.ended_at||Date.now()/1000)-r.started_at)):'—'}</td>`}</tr>`).join('')}</tbody></table>`;}
function overview(data){
 state.data=data;const inv=data.inventory,run=data.runtime,active=data.active;
 const removed=data.deletion_breakdown||{};
 const removedLines=[bytes(inv.deleted.bytes)+' 目标条目大小',...Object.entries({manual:'手动删除',syncthing:'同步删除',unknown:'来源未知'}).map(([key,label])=>{const group=removed[key]||{};return label+' '+num(group.files||0)+' 个 · '+bytes(group.bytes||0)})];
 $('metrics').innerHTML=[
  ['待处理照片',num(inv.pending.files),'个',bytes(inv.pending.bytes)+' 等待处理','↗',''],
  ['已映射到目标',num(inv.linked.files),'个',bytes(inv.linked.bytes)+' 目标库存','⇄',''],
  ['已完成批次',num(data.batches.completed),'批','共 '+num(data.batches.total)+' 个历史批次','✓','green'],
  ['已移除目标',num(inv.deleted.files),'个',removedLines,'◷','purple']
 ].map(([label,value,unit,foot,icon,color])=>`<div class="metric${Array.isArray(foot)?' metric-deleted':''}"><div class="metric-top">${label}<span class="metric-symbol ${color}">${icon}</span></div><div class="metric-value">${value}<small>${unit}</small></div><div class="metric-foot"${Array.isArray(foot)?' title="按库存记录大小统计，不代表 NAS 实际释放空间"':''}>${Array.isArray(foot)?foot.map(line=>`<div>${esc(line)}</div>`).join(''):esc(foot)}</div></div>`).join('');
 if(active){
  const index=phaseOrder.indexOf(active.phase),groups=[['mapping','映射'],['sync','同步'],['backup','备份'],['release','释放'],['observe','观察'],['final_sync','完成']];
  const currentGroup=active.phase==='release_uncertain'||active.phase==='deletions'?3:active.phase==='cleanup'?4:Math.max(0,groups.findIndex(([p])=>p===active.phase));
  const control=run.controller||{},error=control.state==='error';
  $('batch-badge').className='badge '+(error?'red':run.controller_stale?'amber':'blue');$('batch-badge').textContent=error?'异常 · 已暂停':run.controller_stale?'等待状态更新':active.phase_label;
  $('active-batch').innerHTML=`<div class="batch-meta"><span class="batch-id">BATCH #${esc(active.id.slice(0,8).toUpperCase())}</span><small>${esc(date(active.started_at))} 开始</small></div><p class="batch-description">${error?esc(control.error):active.phase==='observe'?`同步与备份稳定确认 ${Number(active.observations||0)} / 3 次，每次至少间隔 5 分钟。`:'本批成员已冻结，新发现的照片将在下一批处理。'}</p><div class="pipeline">${groups.map(([p,label],i)=>`<div class="step ${i<currentGroup?'done':i===currentGroup?'current':''}"><span class="step-dot">${i<currentGroup?'✓':i+1}</span><span class="step-label">${label}</span></div>`).join('')}</div><div class="batch-stats"><div class="batch-stat"><strong>${num(active.file_count)}</strong><small>本批文件</small></div><div class="batch-stat"><strong>${active.backed_up==null?'—':num(active.backed_up)}</strong><small>整体备份确认</small></div><div class="batch-stat"><strong>${esc(elapsed(Date.now()/1000-(active.started_at||Date.now()/1000)))}</strong><small>本批已用时</small></div></div><div class="batch-bottom"><span>${active.next_check&&active.phase==='observe'?'下次确认不早于 '+esc(date(active.next_check,true)):'当前阶段：'+esc(active.phase_label)}</span><button class="text-link" data-batch="${esc(active.id)}">批次详情 ↗</button></div>`;
 }else{$('batch-badge').className='badge green';$('batch-badge').textContent='当前无活动批次';$('active-batch').innerHTML=empty('正在等待下一批照片 · 源扫描独立运行');}
 liveSections(run);
 $('recent').innerHTML=batchTable(data.recent,true);
 updateSyncProgress(run);
 runtimeNotice(run);
 if($('error-banner').hidden&&(data.statistics_stale||data.inventory_updated_at&&Date.now()/1000-data.inventory_updated_at>30))banner('库存统计正在更新；设备状态按最近检查结果显示。');
 $('updated').textContent='最近更新 '+date(data.time)+(data.inventory_updated_at?' · 库存统计 '+ago(data.inventory_updated_at):'');$('refresh-label').textContent=(data.refresh_seconds||10)+'s';
}
function pixelStatus(sync){
 const known=sync.completion!=null&&Number.isFinite(Number(sync.completion));
 const percent=known?(Math.floor(Math.max(0,Math.min(100,Number(sync.completion)))*10)/10).toFixed(1)+'%':'未记录';
 const total=sync.total_bytes,remaining=sync.need_remote_bytes;
 const received=total!=null&&remaining!=null?Math.max(0,Math.min(Number(total),Number(total)-Number(remaining))):null;
 const amount=value=>value==null?'—':Number(value)>=1073741824?(Number(value)/1073741824).toFixed(2)+' GiB':bytes(value);
 const online=sync.connected===true;
 let label='未检查',color='neutral';
 if(sync.error){label='读取异常';color='amber';}
 else if(sync.connected===false){label='离线';color='red';}
 else if(['invalid','error','paused'].includes(sync.remote_state)){label=sync.remote_state==='paused'?'同步暂停':'文件夹异常';color='red';}
 else if(online){
  label=Number(sync.need_remote_deletes)>0?'同步删除中':known&&Number(sync.completion)>=100&&sync.need_remote===0&&remaining===0&&sync.remote_state==='valid'?'接收完成':known&&Number(sync.completion)>=100?'等待确认':known?'接收中':'等待进度';
  color=label==='接收完成'?'green':'blue';
 }
 return ['▯','手机 同步',[
  (online?'在线':sync.connected===false?'离线':'连接状态待确认')+((!online||sync.error)?' · 上次进度 ':' · 进度 ')+percent,
  '已接收 '+amount(received)+' / '+amount(total)+' · 剩余 '+amount(remaining),
  '待同步 '+(sync.need_remote==null?'—':num(sync.need_remote))+' 项 · 待删除 '+(sync.need_remote_deletes==null?'—':num(sync.need_remote_deletes))+' 项'
 ],label,color,sync.progress_checked_at||sync.checked_at];
}
function batteryRow(battery){
 const known=typeof battery.level==='number'&&Number.isFinite(battery.level)&&battery.level>=0&&battery.level<=100;
 const stale=!battery.measured_at||Date.now()/1000-battery.measured_at>600;
 const valid=known&&battery.state==='ok'&&!stale;
 const power=battery.status==='full'?'已充满':battery.charging?'充电中':battery.power_connected?'已接电 · 未充电':'未充电';
 const label=valid?`${battery.level}% · ${power}`:known?`上次 ${battery.level}%`:'电量未知';
 const desc=valid?'跟随控制器采集手机电量':battery.state==='error'?'读取失败，保留上次电量':known?'数据未更新，保留上次电量':'等待手机电量数据';
 return ['▯','手机 电量',desc,label,valid?(battery.level<=20?'amber':battery.charging||battery.status==='full'?'green':'blue'):'neutral',battery.measured_at];
}
function liveSections(run){
 state.latestRuntime=run;
 const sync=run.syncthing||{},photos=run.photos||{},storage=run.storage||{},ctrl=run.controller||{};
 const health=[
  ['▱','NAS 存储','源目录与目标目录所在的 NFS',storage.state==='mounted'?'已挂载':storage.state==='error'?'挂载异常':'未检查',storage.state==='mounted'?'green':storage.state==='error'?'red':'neutral',storage.checked_at],
  ['⇄','NAS 端 Syncthing',sync.need_local==null?'本机状态待检查':`本机待同步 ${num(sync.need_local)} 项${sync.need_local_bytes!=null?' · '+bytes(sync.need_local_bytes):''}`,sync.error?'服务异常':sync.local_state==='scanning'?'扫描中':sync.local_state==='syncing'?'同步中':sync.local_state==='idle'&&sync.need_local===0?'本机就绪':sync.local_state?'等待同步':'未检查',sync.error?'red':sync.local_state==='idle'&&sync.need_local===0?'green':sync.local_state?'blue':'neutral',sync.checked_at],
  pixelStatus(sync),
  batteryRow(run.battery||{}),
  ['◉','Google 相册',[(photos.state!=='error'&&Array.isArray(photos.details)?photos.details.filter(s=>!['备份','Backup'].includes(s)).join(' · '):'')||photos.message||'由手机相册整体状态确认',ctrl.phase==='sync'?'本批仍在同步，以上为手机当前内容的备份状态':''].filter(Boolean),{complete:'备份完成',syncing:'备份中',blocked:'已暂停',error:'手机异常',unknown:'状态未知'}[photos.state]||'未检查',photos.state==='complete'?'green':['error','blocked'].includes(photos.state)?'red':photos.state==='syncing'?'blue':'neutral',photos.checked_at],
  ['◷','备份控制器','状态检查 '+ago(ctrl.checked_at),run.controller_stale?'状态未更新':ctrl.state==='error'?'遇到异常':ctrl.state==='stopped'?'已停止':ctrl.state?'运行中':'未连接',run.controller_stale?'amber':ctrl.state==='error'?'red':ctrl.state==='stopped'?'neutral':ctrl.state?'green':'neutral',ctrl.checked_at]
 ];
 $('health').innerHTML=health.map(([icon,name,desc,label,color,stamp])=>`<div class="health-row"><span class="health-icon">${icon}</span><div class="health-name">${name}<small title="${esc(date(stamp))}">${(Array.isArray(desc)?desc:[desc]).map(line=>esc(line)).join('<br>')}${stamp?' · '+esc(ago(stamp)):''}</small></div>${badge(label,color)}</div>`).join('');
 const scan=run.scan||{},stats=scan.stats||{};
 $('scan-description').textContent=scan.state==='error'?'扫描暂时失败：'+(scan.error||'请查看运行动态'):scan.state==='complete'?'本次扫描已完成，新增文件将由下一批接管。':'独立扫描源目录，新增照片将在下一批处理。';
 $('scan-stats').innerHTML=`<div class="scan-number"><strong>${num(stats.files)}</strong><small>本次已扫描文件</small></div><div class="scan-number"><strong>${bytes(stats.size_bytes)}</strong><small>本次已扫描大小</small></div><div class="scan-number"><strong>${num(stats.new_pending)}</strong><small>新增待处理</small></div><span class="scan-pill">${badge({scanning:'扫描中',complete:'本轮完成',error:'扫描异常'}[scan.state]||'等待状态',scan.state==='scanning'?'blue':scan.state==='complete'?'green':scan.state==='error'?'red':'neutral')}</span>`;
 const events=(run.events||[]).slice(-12).reverse();$('events').innerHTML=events.length?events.map(e=>`<div class="event"><span class="event-dot ${e.kind.includes('recovered')?'green':''}"></span><div class="event-text">${esc(e.message)}<span class="event-time">${esc(date(e.time))}${e.batch_id?' · #'+esc(e.batch_id.slice(0,8)):''}</span></div></div>`).join(''):empty('运行正常，暂时没有异常事件');
}
function pager(view,data){const pages=Math.max(1,Math.ceil(data.total/data.page_size));$(view+'-pagination').innerHTML=`<span>共 ${num(data.total)} 条记录</span><span>${data.page} / ${num(pages)} 页</span><button data-page-view="${view}" data-page="${data.page-1}" ${data.page<=1?'disabled':''}>上一页</button><button data-page-view="${view}" data-page="${data.page+1}" ${data.page>=pages?'disabled':''}>下一页</button>`;}
function filePath(path,id){const parts=String(path).split('/'),name=parts.pop();return `<button class="file-link" data-file="${Number(id)}" title="${esc(path)}">${esc(name)}</button><span class="path-dir">${esc(parts.join('/')||'/')}</span>`;}
async function list(view,{background=false}={}){
 if(background&&state.listPending&&state.listView===view)return state.listPending;
 const version=++state.requestVersion;
 state.listController?.abort();
 const controller=state.listController=new AbortController();
 let path='/api/'+view,params=new URLSearchParams({page:state.pages[view],size:30});
 if(view==='files'){params.set('q',$('files-query').value);params.set('status',$('files-status').value);if(state.batchFilter)params.set('batch',state.batchFilter);}
 if(view==='audits'){params.set('q',$('audits-query').value);params.set('origin',$('audits-origin').value);params.set('state',$('audits-state').value);}
 const table=$(view+'-table');table.setAttribute('aria-busy','true');table.classList.add('is-loading');
 if(!table.innerHTML)table.innerHTML='<div class="loading-state">正在读取…</div>';
 const work=(async()=>{
  const data=await api(path+'?'+params,{signal:controller.signal});
  if(version!==state.requestVersion||view!==state.view)return;
  const visible=view==='files'?{total:data.total,page:data.page,items:data.items.map(r=>[r.id,r.captured_at,r.file_created_at,r.source_path,r.size_bytes,r.status,r.backup_state,r.deletion_origin,r.source_present])}:data;
  const signature=params.toString()+'|'+JSON.stringify(visible);
  if(view!=='batches'&&state.listSignatures[view]===signature)return;
  state.listSignatures[view]=signature;
 if(view==='batches'){$('batches-table').innerHTML=batchTable(data.items);$('batches-count').textContent=num(data.total)+' 个批次';}
 if(view==='files'){$('batch-filter').hidden=!state.batchFilter;$('batch-filter').innerHTML=state.batchFilter?`仅显示批次 #${esc(state.batchFilter.slice(0,8))} <button class="text-link" id="clear-batch">清除筛选 ×</button>`:'';$('files-table').innerHTML=data.items.length?`<table><thead><tr><th>文件 / 路径</th><th>拍摄时间</th><th>大小</th><th>状态</th><th>备份确认</th><th>删除来源</th><th>源文件</th></tr></thead><tbody>${data.items.map(r=>`<tr><td>${filePath(r.source_path,r.id)}</td><td class="mono">${esc(captureDate(r))}</td><td>${bytes(r.size_bytes)}</td><td>${badge(statusLabel[r.status]||r.status,r.status==='linked'?'blue':r.status==='deleted'?'green':'neutral')}</td><td>${esc(backupLabel[r.backup_state]||r.backup_state)}</td><td>${r.status==='deleted'?esc(originLabel[r.deletion_origin]||r.deletion_origin):'—'}</td><td>${r.source_present?'库存最近扫描存在':'最近扫描未发现'}</td></tr>`).join('')}</tbody></table>`:empty('没有符合筛选条件的文件');}
 if(view==='audits')$('audits-table').innerHTML=data.items.length?`<table><thead><tr><th>事件 / 文件</th><th>来源</th><th>执行结果</th><th>原因</th><th>发生时间</th></tr></thead><tbody>${data.items.map(r=>`<tr><td><span class="subtle">#${Number(r.id)}</span> <span title="${esc(r.source_path)}">${esc(r.source_path.length>52?'…'+r.source_path.slice(-51):r.source_path)}</span></td><td>${esc(originLabel[r.origin]||r.origin)}</td><td>${badge(statusLabel[r.state]||r.state,r.state==='deleted'?'green':r.state==='error'?'red':'amber')}</td><td title="${esc(r.reason)}">${esc(reasonLabel[r.reason]||(String(r.reason||'').startsWith('remote_tombstone:')?'Pixel 端删除证据':r.reason)||'—')}</td><td>${esc(date(r.created_at,true))}</td></tr>`).join('')}</tbody></table>`:empty('没有符合条件的删除记录');
  pager(view,data);
 })();
 state.listPending=work;state.listView=view;
 try{return await work;}catch(error){if(error.name!=='AbortError')throw error;}
 finally{if(version===state.requestVersion){table.removeAttribute('aria-busy');table.classList.remove('is-loading');state.listPending=null;}}
}

function openDrawer(){state.drawerVersion++;state.drawerOpener=document.activeElement;$('drawer-overlay').hidden=false;$('drawer').hidden=false;$('drawer-content').innerHTML='<div class="loading-state">正在读取详情…</div>';document.body.style.overflow='hidden';$('close-drawer').focus();return state.drawerVersion;}
function closeDrawer(){state.drawerVersion++;$('drawer-overlay').hidden=true;$('drawer').hidden=true;document.body.style.overflow='';state.drawerOpener?.focus();}
async function batchDetail(id){const version=openDrawer();try{const row=await api('/api/batches/'+encodeURIComponent(id));if($('drawer').hidden||version!==state.drawerVersion)return;const durations={...row.phase_seconds};if(row.phase!=='complete'&&row.phase_started_at)durations[row.phase]=(durations[row.phase]||0)+Math.max(0,Date.now()/1000-row.phase_started_at);const maximum=Math.max(1,...Object.values(durations).map(Number));$('drawer-content').innerHTML=`<h2 id="drawer-title">批次详情 ${phaseBadge(row)}</h2><div class="mono">${esc(row.id)}</div><div class="detail-grid">${detailItem('开始时间',date(row.started_at))}${detailItem('结束时间',row.ended_at?date(row.ended_at):'正在进行')}${detailItem('本批文件',num(row.file_count)+' 个')}${detailItem('整体备份确认',row.backed_up==null?'未记录':num(row.backed_up)+' 个')}${detailItem('手动删除成功',row.summary?num(row.summary.manual_deleted)+' 个':'待批次结束统计')}${detailItem('文件变化待核查',num(row.deferred)+' 个')}${detailItem('观察次数',num(row.observations)+' / 3')}${detailItem('总耗时',row.started_at?elapsed((row.ended_at||Date.now()/1000)-row.started_at):'未记录')}</div><div class="detail-note">备份数按相册整体完成状态确认，不代表本轮新增上传量。${row.timing_partial?' 此批在统计升级前已开始，历史阶段耗时没有补造。':''}</div><h3>各阶段耗时 <span class="subtle">含等待与重启间隔</span></h3>${Object.entries(phaseLabels).filter(([p])=>p!=='complete').map(([p,label])=>`<div class="phase-time-row"><span>${label}</span><div class="phase-time-track"><div class="phase-time-bar" style="width:${Math.max(0,Math.min(100,Number(durations[p]||0)/maximum*100))}%"></div></div><span>${elapsed(durations[p]||0)}</span></div>`).join('')}<button class="primary-button" data-batch-files="${esc(row.id)}">查看本批文件 ↗</button>${row.summary?.body?`<h3>批次完成报告</h3><div class="detail-note">${esc(row.summary.body)}</div>`:''}`;}catch(error){if(version===state.drawerVersion)$('drawer-content').innerHTML=empty(error.message);}}
async function fileDetail(id){const version=openDrawer();try{const row=await api('/api/files/'+Number(id));if($('drawer').hidden||version!==state.drawerVersion)return;$('drawer-content').innerHTML=`<h2 id="drawer-title">${esc(row.source_path.split('/').pop())}</h2><div class="mono">${esc(row.source_path)}</div><div class="detail-grid">${detailItem('文件大小',bytes(row.size_bytes))}${detailItem('目标状态',statusLabel[row.status]||row.status)}${detailItem('备份确认方式',backupLabel[row.backup_state]||row.backup_state)}${detailItem('源库存状态',row.source_present?'最近扫描存在':'最近扫描未发现')}${detailItem('拍摄时间',captureDate(row))}${detailItem('源修改时间',row.mtime_ns?date(Number(row.mtime_ns)/1e9):'未记录')}${detailItem('建立链接时间',date(row.linked_at))}${detailItem('检测删除时间',date(row.deleted_at))}${detailItem('删除来源',originLabel[row.deletion_origin]||row.deletion_origin)}</div>${row.last_error?`<div class="detail-note">${esc(row.last_error)}</div>`:''}${row.batch_id?`<button class="text-link" data-batch="${esc(row.batch_id)}">所属批次 ${esc(row.batch_id.slice(0,8).toUpperCase())} ↗</button>`:''}<h3>相关删除审计</h3>${row.audits.length?row.audits.map(a=>`<div class="event"><span class="event-dot ${a.state==='deleted'?'green':''}"></span><div class="event-text">${esc(originLabel[a.origin]||a.origin)} · ${esc(statusLabel[a.state]||a.state)}<br>${esc(reasonLabel[a.reason]||(String(a.reason||'').startsWith('remote_tombstone:')?'Pixel 端删除证据':a.reason)||'无附加说明')}${a.error_message?'<br>'+esc(a.error_message):''}<span class="event-time">#${Number(a.id)} · ${esc(date(a.created_at))}</span></div></div>`).join(''):empty('此文件尚无删除审计')}`;}catch(error){if(version===state.drawerVersion)$('drawer-content').innerHTML=empty(error.message);}}
const viewCopy={settings:['运行配置','按你的节奏，安排备份。','调整同步参数，保存后自动应用并持久保留。'],overview:['同步总览','每一份回忆，都妥善保存。','从 NAS 到 Pixel，再到云端，查看照片同步与备份的每一步。'],batches:['批次历史','回看每一批，保存每一步。','从开始到结束，查看备份结果与各阶段累计耗时。'],files:['文件明细','每一个文件，都有迹可循。','从待处理到备份完成，检索照片路径与库存状态。'],audits:['删除审计','清理目标空间，保留源照片。','核对删除来源、执行结果，以及每次清理留下的审计记录。']};
async function changeView(){
 const name=location.hash.slice(1),view=viewCopy[name]?name:'overview';state.view=view;
 Object.keys(viewCopy).forEach(v=>$(v).hidden=v!==view);
 document.querySelectorAll('[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
 const [title,heading,description]=viewCopy[view];$('crumb').textContent=title;$('page-title').textContent=heading;$('page-description').textContent=description;
 await refresh({background:false});
}
function refreshOverview(){
 if(state.overviewPending)return state.overviewPending;
 const work=api('/api/overview',{cacheMs:0}).then(data=>{state.data=data;if(state.view==='overview')overview(data);});
 state.overviewPending=work.finally(()=>{state.overviewPending=null;});return state.overviewPending;
}
function refreshRuntime(){
 if(state.runtimePending)return state.runtimePending;
 const work=api('/api/runtime',{cacheMs:0}).then(data=>{
  const run=data.runtime;
  if(state.view==='overview'){
   liveSections(run);
   if(!state.data&&run.controller?.batch_id&&run.controller.phase!=='complete'){
    const ctrl=run.controller,label=phaseLabels[ctrl.phase]||ctrl.phase;
    $('batch-badge').className='badge '+(ctrl.state==='error'?'red':'blue');$('batch-badge').textContent=label;
    $('active-batch').innerHTML=`<div class="batch-meta"><span class="batch-id">BATCH #${esc(ctrl.batch_id.slice(0,8).toUpperCase())}</span></div><p class="batch-description">当前阶段：${esc(label)}。文件数量与耗时正在读取，设备状态已可查看。</p><button class="text-link" data-batch="${esc(ctrl.batch_id)}">批次详情 ↗</button>`;
   }
  }
  if(state.view==='overview')updateSyncProgress(run);
  runtimeNotice(run);
  if(state.view==='overview'&&$('error-banner').hidden&&state.data?.inventory_updated_at&&Date.now()/1000-state.data.inventory_updated_at>30)banner('库存统计正在更新；设备状态按最近检查结果显示。');
  $('updated').textContent='最近更新 '+date(data.time)+(state.view==='overview'&&state.data?.inventory_updated_at?' · 库存统计 '+ago(state.data.inventory_updated_at):'');
 });
 state.runtimePending=work.finally(()=>{state.runtimePending=null;});return state.runtimePending;
}

async function refresh({background=true}={}){
 const view=state.view;state.refreshPending[view]=(state.refreshPending[view]||0)+1;$('refresh').disabled=true;
 try{
  const jobs=view==='settings'?[refreshSettings({render:!background})]:view==='overview'?[refreshOverview(),refreshRuntime()]:[refreshRuntime(),list(view,{background})];
  const results=await Promise.allSettled(jobs);
  const failure=results.find(r=>r.status==='rejected'&&r.reason.name!=='AbortError');
  if(failure&&view===state.view){banner(failure.reason.message+'；保留上次数据，稍后自动重试。');$('updated').textContent='更新失败 · '+new Date().toLocaleTimeString('zh-CN');}
 }finally{state.refreshPending[view]--; $('refresh').disabled=(state.refreshPending[state.view]||0)>0;}
}

document.addEventListener('click',event=>{const t=event.target.closest('button');if(!t)return;if(t.dataset.batch)batchDetail(t.dataset.batch);if(t.dataset.file)fileDetail(t.dataset.file);if(t.dataset.pageView){state.pages[t.dataset.pageView]=Number(t.dataset.page);list(t.dataset.pageView).catch(e=>banner(e.message));}if(t.dataset.batchFiles){state.batchFilter=t.dataset.batchFiles;state.pages.files=1;closeDrawer();if(state.view==='files')list('files').catch(e=>banner(e.message));else location.hash='files';}if(t.id==='clear-batch'){state.batchFilter='';state.pages.files=1;list('files').catch(e=>banner(e.message));}});
$('close-drawer').addEventListener('click',closeDrawer);$('drawer-overlay').addEventListener('click',closeDrawer);
document.addEventListener('keydown',event=>{if($('drawer').hidden)return;if(event.key==='Escape')closeDrawer();if(event.key==='Tab'){const focusable=$('drawer').querySelectorAll('button,a,input,select');const first=focusable[0],last=focusable[focusable.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}});
let debounce;['files-query','audits-query'].forEach(id=>$(id).addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(()=>{const view=id.split('-')[0];state.pages[view]=1;list(view).catch(e=>banner(e.message));},300);}));
['files-status','audits-origin','audits-state'].forEach(id=>$(id).addEventListener('change',()=>{const view=id.split('-')[0];state.pages[view]=1;list(view).catch(e=>banner(e.message));}));
let settingsSnapshot=null,settingsDirty=false,settingsSaving=false;
const settingGroups={scan:'源目录扫描',mapping:'新批次与电量',monitor:'检查频率',observation:'清理前观察',bark:'Bark 通知'};
async function refreshSettings({render=false}={}){
 const data=await api('/api/settings',{cacheMs:0});
 $('settings-status').textContent=data.controller_stale?'控制器离线':data.error?'应用失败':data.applied?'已生效':'已保存 · 等待应用';
 $('settings-status').className='badge '+(data.applied&&!data.controller_stale?'green':'neutral');
 if(data.error)$('settings-message').textContent=data.error;
 if(!settingsSnapshot||render&&!settingsDirty&&!settingsSaving){
  settingsSnapshot=data;
  $('settings-fields').innerHTML=Object.entries(data.fields).map(([group,fields])=>`<fieldset><legend>${esc(settingGroups[group]||group)}</legend><div class="settings-grid">${Object.entries(fields).map(([key,[label,min,max]])=>`<label>${esc(label)}<input data-group="${esc(group)}" data-key="${esc(key)}" type="${min===null?'checkbox':'number'}" ${min===null?(data.values[group][key]?'checked':''):`min="${min}" max="${max}" step="1" value="${data.values[group][key]}" required`}></label>`).join('')}</div></fieldset>`).join('');
 }
}
$('settings-form').addEventListener('input',()=>{settingsDirty=true;$('settings-message').textContent='有未保存的修改';});
$('settings-reload').addEventListener('click',()=>{settingsDirty=false;$('settings-message').textContent='';refreshSettings({render:true}).catch(e=>{$('settings-message').textContent=e.message;});});
$('settings-form').addEventListener('submit',async event=>{
 event.preventDefault();if(!settingsSnapshot||settingsSaving)return;
 const values={};$('settings-fields').querySelectorAll('input').forEach(input=>{(values[input.dataset.group]??={})[input.dataset.key]=input.type==='checkbox'?input.checked:Number(input.value);});
 settingsSaving=true;$('settings-save').disabled=true;$('settings-reload').disabled=true;
 try{
  const response=await fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-PicSync-Settings':'1'},body:JSON.stringify({values,revision:settingsSnapshot.revision}),signal:AbortSignal.timeout(15000)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||'保存失败');
  settingsSnapshot=data;settingsDirty=false;$('settings-message').textContent='已保存，等待控制器应用';
  await refreshSettings();
 }catch(error){$('settings-message').textContent=error.message;}
 finally{settingsSaving=false;$('settings-save').disabled=false;$('settings-reload').disabled=false;}
});
$('refresh').addEventListener('click',()=>{state.apiCache.clear();refresh({background:false});});window.addEventListener('hashchange',changeView);changeView();setInterval(()=>{if(!document.hidden)refresh();},10000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
