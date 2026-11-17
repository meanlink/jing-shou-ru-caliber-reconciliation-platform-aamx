import {api} from './api/client.js';
import {session} from './stores/session.js';
import {workspace} from './stores/workspace.js';
import {shell} from './components/common/shell.js';
import {route} from './router/index.js';
import {login} from './pages/login.js';
import {overview} from './pages/overview.js';
import {dataPage,bindData} from './pages/datasets.js';
import {workbench,bindWorkbench} from './pages/workbench.js';
import {results,bindResults} from './pages/results.js';
import {reportPage,bindReports} from './pages/reports.js';
import {reconciliationPage,bindReconciliation} from './pages/reconciliations.js';
import {esc} from './utils/dom.js';
async function render(){
 const app=document.querySelector('#app');
 try{
   if(!workspace.meta){workspace.meta=await api('/meta');document.title=workspace.meta.name;document.documentElement.style.setProperty('--accent',workspace.meta.accent||'oklch(48% 0.13 145)');document.body.dataset.style=workspace.meta.style||'ledger';}
   const {page,id}=route();
   if(!session.token||page==='login'){login();return;}
   // Mark the outgoing page inert while its replacement loads: a stale table
   // must never accept edits or a save aimed at the next view.
   if(app.firstElementChild){
     app.classList.add('loading');
     app.querySelectorAll('input,select,textarea,button').forEach(el=>{el.disabled=true;});
   }
   const pages={overview,datasets:dataPage,workbench,results:()=>results(id),reports:reportPage,reconciliations:()=>reconciliationPage(id)};
   const content=await (pages[page]||overview)();app.innerHTML=shell(page,content);app.classList.remove('loading');
   document.querySelector('#logout').onclick=()=>{session.clear();location.hash='#/login';render();};
   if(page==='datasets')bindData(render);if(page==='workbench')bindWorkbench();if(page==='results')bindResults(render);if(page==='reports')bindReports(render);if(page==='reconciliations')bindReconciliation(render);
 }catch(e){app.classList.remove('loading');app.innerHTML='<div class="fatal"><h1>暂时无法打开工作室</h1><p>'+esc(e.message)+'</p><button onclick="location.reload()">重新连接</button></div>';}
}
addEventListener('hashchange',render);render();
