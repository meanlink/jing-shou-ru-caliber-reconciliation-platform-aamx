import {datasets,reconciliations} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,fmt,notify} from '../utils/dom.js';
import {ReconciliationStatus} from '../constants/enums.js';
import {submit} from '../hooks/forms.js';

const COLUMNS=()=>workspace.meta.columns;
const isNumeric=key=>COLUMNS().find(c=>c.key===key)?.type==='number';
const STATUS_CLASS={conflict:'status failed',stale:'status failed',merging:'status',ready:'status',closed:'status completed'};
const statusOf=d=>{const key=ReconciliationStatus[d.display_status]?d.display_status:ReconciliationStatus[d.status]?d.status:'open';return [ReconciliationStatus[key],STATUS_CLASS[key]||'status'];};
let view='finance';
// Unsaved edits are kept per case and side so an in-flight page refresh can
// never silently discard what the user just typed.
let drafts={case:null,finance:null,channel:null};
let activeCase=null;

const rowKey=r=>r.day+' · '+r.channel;
const rowSummary=row=>COLUMNS().filter(c=>c.type==='number').map(c=>c.label+' '+fmt(row[c.key])).join(' / ');

function copyEditor(records,dirty,editable,side,revision,caseId){
 return '<div class="copy-editor" data-side="'+esc(side)+'" data-revision="'+esc(revision)+'" data-case="'+esc(caseId)+'"><div class="copy-toolbar"><span>共 '+records.length+' 行 · 保存整表即记录一个副本版本</span><span>'+(editable?'<button class="button small" id="copy-add">新增一行</button>':'')+' <button class="button small" id="copy-reset" '+(dirty?'':'disabled')+'>放弃未保存修改</button></span></div><div class="table-wrap edit-wrap"><table class="edit-table"><thead><tr>'+COLUMNS().map(c=>'<th>'+esc(c.label)+'</th>').join('')+'<th></th></tr></thead><tbody>'+records.map((r,index)=>'<tr data-row="'+index+'">'+COLUMNS().map(c=>'<td><input data-field="'+esc(c.key)+'" value="'+esc(r[c.key])+'" '+(isNumeric(c.key)?'inputmode="decimal"':'')+(editable?'':' disabled')+'></td>').join('')+'<td>'+(editable?'<button class="text-button" data-remove="'+index+'">删除</button>':'')+'</td></tr>').join('')+'</tbody></table></div></div>';
}

function readEditor(){
 const rows=[];
 document.querySelectorAll('.copy-editor .edit-table tbody tr').forEach(tr=>{
  const row={};tr.querySelectorAll('input[data-field]').forEach(input=>{row[input.dataset.field]=input.value;});
  rows.push(row);
 });
 return rows;
}

function conflictTable(detail){
 const conflicts=detail.conflicts||[];
 if(detail.merge_stale)return '<p class="warning">副本已在合并后更新，当前合并结果已过期。请先重新合并，再查看待裁决项。</p>';
 if(!conflicts.length)return '<p class="empty">当前合并无待裁决项。</p>';
 const editable=detail.status!=='closed'&&session.user.role==='admin';
 return '<div class="table-wrap"><table><thead><tr><th>行</th><th>字段</th><th>共同底账</th><th>财务副本</th><th>渠道副本</th><th>裁决</th></tr></thead><tbody>'+conflicts.map((c,i)=>{
  const fieldLabel=c.field?(COLUMNS().find(x=>x.key===c.field)?.label||c.field):'整行（删除 / 修改相撞）';
  const show=v=>v===null?'<span class="void">— 已删除</span>':typeof v==='object'?'<span class="row-peek">'+esc(rowSummary(v))+'</span>':esc(fmt(v));
  return '<tr data-conflict="'+i+'"><td>'+esc(rowKey(c.row))+'</td><td>'+esc(fieldLabel)+'</td><td>'+show(c.base)+'</td><td>'+show(c.finance)+'</td><td>'+show(c.channel)+'</td><td>'+(editable?'<label class="inline"><input type="radio" name="c'+i+'" value="finance"> 财务</label><label class="inline"><input type="radio" name="c'+i+'" value="channel"> 渠道</label><button class="button small" data-adjudicate="'+i+'">裁决</button>':(session.user.role==='admin'?'<span class="hint">结案后不可裁决</span>':'待负责人裁决'))+'</td></tr>';
 }).join('')+'</tbody></table></div>';
}

function mergePanel(detail){
 const m=detail.merge;
 if(!m)return '<p class="empty">尚未执行合并。两侧副本保存后，点击“逐格合并”。</p>';
 const stale=m.status==='failed';
 return '<div class="merge-summary"><div><span>合并 #'+m.id+'</span><strong>'+esc({running:'合并中',completed:'已完成',failed:'已过期'}[m.status]||m.status)+'</strong></div><div><span>依据版本</span><strong>财务 r'+m.finance_revision+' / 渠道 r'+m.channel_revision+'</strong></div><div><span>合并后行数</span><strong>'+esc(m.stats?.rows_base??'—')+'</strong></div><div><span>财务改动格</span><strong>'+esc(m.stats?.cells_from_finance??0)+'</strong></div><div><span>渠道改动格</span><strong>'+esc(m.stats?.cells_from_channel??0)+'</strong></div><div><span>已裁决格</span><strong>'+esc(m.stats?.cells_adjudicated??0)+'</strong></div><div><span>待裁决</span><strong>'+esc(m.stats?.conflicts??0)+'</strong></div></div>'+(stale?'<p class="warning">'+esc(m.error||'过期合并拒绝发布，请重新合并。')+'</p>':'')+'<p class="hint">合并指纹 '+esc((m.digest||'').slice(0,24))+'… · 合并结果仅由服务端逐格计算，不采用整表覆盖。</p>';
}

function publishPanel(detail){
 const d=detail;
 const isAdmin=session.user.role==='admin';
 const canPublish=isAdmin&&d.display_status==='ready'&&d.merge?.status==='completed'&&!d.merge_stale&&!(d.conflicts||[]).length;
 const mergeStale=!!d.merge_stale;
 const p=d.publish;
 return '<div class="publish-panel"><div><p class="eyebrow">PUBLISH / CLOSE</p><h2>发布并结案</h2><p>仅当合并结果基于两侧最新版本、全账通过校验且无待裁决项时，才同时结案并生成可分析批次。重复发布返回同一批次，不新增记录。</p>'+(mergeStale?'<p class="warning">当前合并快照已过期，需重新合并后再发布。</p>':'')+(d.conflicts?.length?'<p class="warning">仍有 '+d.conflicts.length+' 项待裁决，发布会拒绝。</p>':'')+'</div><div class="publish-actions">'+(p?'<div class="publish-receipt"><span>已结案 · 批次来源可追溯</span><strong>'+esc(p.batch_name)+'</strong><p>分析批次 #'+p.dataset_id+' · '+p.batch_rows+' 行 · SHA256 '+esc((p.batch_checksum||'').slice(0,20))+'…</p><p>合并 #'+p.merge_id+' 于 '+date(p.created_at)+' 发布</p><a class="button small" href="#/workbench">在新批次上发起分析 ↗</a><a class="button small" href="#/datasets">查看数据批次 ↗</a></div>':(isAdmin?'<button class="primary" id="publish-case" '+(canPublish?'':'disabled')+'>发布并结案，生成分析批次</button>':'<p class="warning">发布结案需要业务负责人权限。</p>'))+'<p class="hint">'+(isAdmin?'发布由业务负责人执行；原账与既有分析保持不变。':'仅业务负责人可发布结案。')+'</p></div></div>';
}

function adjudicationLog(detail){
 const items=detail.adjudications||[];
 if(!items.length)return '<p class="empty">暂无裁决记录。</p>';
 return '<div class="table-wrap"><table><thead><tr><th>行</th><th>字段</th><th>类型</th><th>裁决</th><th>状态</th><th>时间</th></tr></thead><tbody>'+items.map(a=>'<tr><td>'+esc(a.row.day+' · '+a.row.channel)+'</td><td>'+esc(a.field?COLUMNS().find(c=>c.key===a.field)?.label||a.field:'整行')+'</td><td>'+esc(a.kind==='cell'?'同格异改':'删改相撞')+'</td><td>'+esc(a.resolution==='finance'?'采用财务值':'采用渠道值')+'</td><td>'+esc(a.status==='active'?'有效':'已失效')+(a.void_reason?'<small> · '+esc(a.void_reason)+'</small>':'')+'</td><td>'+date(a.created_at)+'</td></tr>').join('')+'</tbody></table></div>';
}

function traceStrip(detail){
 const parts=[['源账',detail.source?('#'+detail.source.id+' '+detail.source.name):'缺失'],['共同底账','SHA256 '+esc((detail.base_checksum||'').slice(0,16))+'…'],['财务副本','r'+detail.finance_revision+' · '+detail.finance_rows+' 行'],['渠道副本','r'+detail.channel_revision+' · '+detail.channel_rows+' 行'],['待裁决',(detail.conflicts||[]).length+' 项'],['结案批次',detail.publish?('#'+detail.publish.dataset_id):'未发布']];
 return '<div class="trace-strip">'+parts.map(([k,v])=>'<div><span>'+esc(k)+'</span><strong>'+v+'</strong></div>').join('')+'</div>';
}

export async function reconciliationPage(id){
 workspace.datasets=await datasets.list();
 const [list]=await Promise.all([reconciliations.list()]);
 const selected=id?await reconciliations.get(id):(list[0]?await reconciliations.get(list[0].id):null);
 const canWrite=session.user.role!=='viewer';
 const detail=selected;
 if(detail&&activeCase!==detail.id){activeCase=detail.id;drafts={case:detail.id,finance:null,channel:null};view=detail.display_status==='closed'?'merge':detail.pending_conflicts?'merge':'finance';}
 const m=workspace.meta;
 const createForm='<form id="case-form"><label>校账名称<input name="name" required minlength="2" value="'+esc(m.name)+' · 九月校账"></label><label>源账批次<select name="dataset_id" required>'+workspace.datasets.map(d=>'<option value="'+d.id+'">'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label><p class="hint">从源账建立校账副本，两侧副本初始都等于共同底账。原账只在建立时读取，永不被覆盖。</p><button type="submit" class="primary" '+(canWrite?'':'disabled')+'>建立校账副本</button></form>';
 const listHtml='<div class="section-title"><h2>对账副本</h2><span>'+list.length+' 个</span></div><div class="batch-list">'+list.map(c=>'<a class="batch-item '+(detail?.id===c.id?'active':'')+'" href="#/reconciliations/'+c.id+'"><strong>'+esc(c.name)+'</strong><span>'+statusOf(c)[0]+' · 财务 r'+c.finance_revision+' / 渠道 r'+c.channel_revision+(c.pending_conflicts?' · '+c.pending_conflicts+' 项待裁决':'')+'</span><small>'+(c.published_dataset_id?'已生成批次 #'+c.published_dataset_id:'尚未结案')+' · 更新 '+date(c.updated_at)+'</small></a>').join('')+(!list.length?'<p class="empty">尚无校账副本，从源账建立第一个。</p>':'')+'</div>';
 let body='';
 if(detail){
  const draft=view==='finance'?drafts.finance:view==='channel'?drafts.channel:null;
  const records=draft||(view==='finance'?detail.finance_records:view==='channel'?detail.channel_records:detail.base_records);
  const dirty=!!draft;
  const editable=canWrite&&detail.status!=='closed'&&(view==='finance'||view==='channel');
  const tabs=[['finance','财务副本'],['channel','渠道副本'],['base','共同底账'],['merge','合并与待裁决'],['log','裁决记录']];
  body='<section class="panel case-head"><div class="section-title"><h2>'+esc(detail.name)+'</h2><span class="'+statusOf(detail)[1]+'">'+statusOf(detail)[0]+'</span></div>'+traceStrip(detail)+'<div class="toolbar">'+
   (detail.status!=='closed'?'<button class="button" id="merge-case" '+(canWrite?'':'disabled')+'>逐格合并两侧副本</button><button class="button" id="resume-case" '+(canWrite?'':'disabled')+'>恢复未完合并</button>':'')+
   '<span class="hint">版本：账本 '+detail.case_version+'；两侧副本各自保存整表，合并逐格对照共同底账。</span></div></section>'+
   '<div class="tabs">'+tabs.map(([key,label])=>'<button class="tab '+(view===key?'active':'')+'" data-tab="'+key+'">'+label+(key==='merge'&&detail.conflicts?.length?' <em>'+detail.conflicts.length+'</em>':'')+'</button>').join('')+'</div>'+
   (view==='merge'?'<section class="panel">'+mergePanel(detail)+'</section><section class="panel"><div class="section-title"><h2>待裁决：同格异改 / 删改相撞</h2><span>负责人逐项选择一侧</span></div>'+conflictTable(detail)+'</section>'+publishPanel(detail):view==='log'?'<section class="panel">'+adjudicationLog(detail)+'</section>':'<section class="panel"><div class="section-title"><h2>'+esc({finance:'财务副本',channel:'渠道副本',base:'共同底账（只读）'}[view])+'</h2><span>'+(editable?'可编辑整表：同行不同字段可分别修改，保存后逐格合并':'只读')+'</span></div>'+(editable?'<div class="toolbar"><button class="primary" id="save-copy">保存'+(view==='finance'?'财务':'渠道')+'副本整表</button><span class="hint">保存会记录版本并令相关旧裁决失效。</span></div>':'')+copyEditor(Array.isArray(records)?records:[],dirty,editable,view,view==='finance'?detail.finance_revision:view==='channel'?detail.channel_revision:0,detail.id)+'</section>');
 }
 return heading('05 / RECONCILIATION','两侧校账与逐格合并','财务和渠道各自改自己的副本，服务端对照共同底账逐格合并；同格异改与删改相撞交负责人裁决，原账与既有分析不被覆盖。')+'<div class="dataset-grid"><section class="panel">'+listHtml+'<div class="section-title"><h2>从源账建立副本</h2></div>'+createForm+'</section><section class="case-panel">'+(body||'<div class="empty">选择或建立一个校账副本开始。</div>')+'</section></div>';
}

export function bindReconciliation(refresh){
 const form=document.querySelector('#case-form');
 if(form)submit(form,async f=>{const c=await reconciliations.create({name:f.get('name'),dataset_id:Number(f.get('dataset_id'))});notify('已从源账建立两侧副本');location.hash='#/reconciliations/'+c.id;});
 document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=async()=>{
  if(button.classList.contains('active'))return;
  view=button.dataset.tab;
  // Replace the stale table with an explicit loading state so an in-flight
  // re-render can never be edited or saved into the newly selected side.
  const panel=document.querySelector('.case-panel');
  if(panel){panel.dataset.busy='1';panel.innerHTML='<div class="empty">正在载入'+esc({finance:'财务副本',channel:'渠道副本',base:'共同底账',merge:'合并与待裁决',log:'裁决记录'}[view])+'…</div>';}
  await refresh();
 });
 // Direct navigation lands on #/reconciliations with no id: fall back to the
 // case that is actually rendered instead of sending NaN to the API.
 const caseId=()=>{const fromHash=Number(location.hash.split('/')[2]);return Number.isFinite(fromHash)&&fromHash>0?fromHash:activeCase;};
 const save=document.querySelector('#save-copy');
 if(save)save.onclick=async()=>{
  try{
   const editor=document.querySelector('.copy-editor');
   if(!editor){notify('副本正在载入，请稍后再保存',true);return;}
   const side=editor.dataset.side;
   const caseIdForSave=Number(editor.dataset.case);
   const expected_revision=Number(editor.dataset.revision);
   if(side!==view||caseIdForSave!==caseId()){notify('页面已切换，请确认当前副本后再保存',true);return;}
   const records=readEditor();
   const result=await reconciliations.saveCopy(caseIdForSave,{side,records,expected_revision});
   drafts[side]=null;
   notify(result.voided_judgements?.length?'副本已保存，'+result.voided_judgements.length+' 项旧裁决已失效，请重新合并':'副本已保存，可执行逐格合并');
   await refresh();
  }catch(e){notify(e.message,true);}
 };
 document.querySelector('#copy-reset')?.addEventListener('click',()=>{drafts={case:activeCase,finance:null,channel:null};refresh();});
 document.querySelector('#copy-add')?.addEventListener('click',()=>{
  const tbody=document.querySelector('.copy-editor .edit-table tbody'),row=document.createElement('tr');
  row.innerHTML=COLUMNS().map(c=>'<td><input data-field="'+esc(c.key)+'" value=""></td>').join('')+'<td><button class="text-button" data-remove>删除</button></td>';
  row.querySelector('[data-remove]').onclick=()=>{row.remove();document.querySelector('#copy-reset').disabled=false;};
  row.querySelectorAll('input').forEach(input=>input.addEventListener('input',()=>{drafts[view]=readEditor();const reset=document.querySelector('#copy-reset');if(reset)reset.disabled=false;}));
  tbody.appendChild(row);row.querySelector('input')?.focus();document.querySelector('#copy-reset').disabled=false;
 });
 document.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{b.closest('tr').remove();document.querySelector('#copy-reset').disabled=false;});
 document.querySelectorAll('.edit-table input').forEach(input=>input.addEventListener('input',()=>{
  const editor=document.querySelector('.copy-editor');
  if(!editor||Number(editor.dataset.case)!==activeCase)return;
  drafts[editor.dataset.side]=readEditor();
  const reset=document.querySelector('#copy-reset');if(reset)reset.disabled=false;
 }));
 document.querySelector('#merge-case')?.addEventListener('click',async()=>{
  try{const r=await reconciliations.merge(caseId());notify(r.conflicts.length?'合并完成，仍有 '+r.conflicts.length+' 项待裁决':'合并完成，两侧改动已逐格合并');view='merge';await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelector('#resume-case')?.addEventListener('click',async()=>{
  try{const r=await reconciliations.resume(caseId());notify(r.resumed?.length?'已恢复 '+r.resumed.length+' 个未完成合并':'没有未完成的合并');view='merge';await refresh();}catch(e){notify(e.message,true);}
 });
 document.querySelectorAll('[data-adjudicate]').forEach(button=>button.onclick=async()=>{
  const index=Number(button.dataset.adjudicate);
  const row=document.querySelector('[data-conflict="'+index+'"]');
  const picked=row.querySelector('input[type=radio]:checked');
  if(!picked){notify('请先选择采用财务值或渠道值',true);return;}
  try{
   const detail=await reconciliations.get(caseId());
   const conflict=detail.conflicts[index];
   if(!conflict){notify('待裁决项已变化，请刷新后再裁决',true);await refresh();return;}
   const r=await reconciliations.adjudicate(caseId(),{row_day:conflict.row.day,row_channel:conflict.row.channel,field:conflict.field,resolution:picked.value});
   notify(r.conflicts.length?'裁决已记录，仍有 '+r.conflicts.length+' 项待裁决':'裁决已记录，全部冲突已解决');
   await refresh();
  }catch(e){notify(e.message,true);}
 });
 document.querySelector('#publish-case')?.addEventListener('click',async()=>{
  const button=document.querySelector('#publish-case');
  if(button.dataset.busy)return;button.dataset.busy='1';button.disabled=true;
  try{
   const detail=await reconciliations.get(caseId());
   const r=await reconciliations.publish(caseId(),{merge_id:detail.merge?.id});
   notify(r.replayed?'该合并已发布，返回既有批次 #'+r.publish.dataset_id:'已结案并生成分析批次 #'+r.publish.dataset_id);
   await refresh();
  }catch(e){notify(e.message,true);}
  finally{delete button.dataset.busy;}
 });
}
