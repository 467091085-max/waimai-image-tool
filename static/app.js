const state={plan:null,style:""};
const $=s=>document.querySelector(s);
const esc=v=>String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
function toast(t){const e=$("#toast");e.textContent=t;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),2200)}
async function api(url,opt={}){const r=await fetch(url,opt);const d=await r.json();if(!r.ok||d.error)throw new Error(d.error||"请求失败");return d}
function tone(t){t=String(t);if(t.includes("复用")||t.includes("完成")||t.includes("一致"))return"good";if(t.includes("缺")||t.includes("定制"))return"bad";return"warn"}
function render(){
 const p=state.plan;$("#items").textContent=p.menu.count;$("#category").textContent=p.category.category;$("#reuse").textContent=p.summary.reuse;$("#cash").textContent=`¥${p.quote.cash}`;
 $("#workflowBox").innerHTML=[
  ["上传 Excel 菜单","已完成",`${p.menu.count} 个菜单项`],["品类识别","已完成",`${p.category.category} · ${p.category.confidence}%`],["菜品标准化","已完成",`${p.standardization.rawItems} → ${p.standardization.canonicalItems} 标准菜`],["展示 5 套风格","已完成",`${p.styles.length} 套风格包`],["客户选风格","进行中",p.selectedStyle],["出图排产","已完成",`复用 ${p.summary.reuse} / 换背景 ${p.summary.bgReplace} / 定制 ${p.summary.custom}`],["付费功能","可配置","水印、餐具、配菜、人工精修"],["导出交付","待确认","zip + Excel 报告"]
 ].map((x,i)=>`<div class="step"><i>${i+1}</i><b>${esc(x[0])}</b><p class="${tone(x[1])}">${esc(x[1])}</p><p>${esc(x[2])}</p></div>`).join("");
 $("#standardBox").innerHTML=[`<div class="card"><b>原始菜单项</b><h2>${p.standardization.rawItems}</h2></div>`,`<div class="card"><b>标准菜数量</b><h2>${p.standardization.canonicalItems}</h2></div>`,`<div class="card"><b>合并别名</b><h2>${p.standardization.aliasMerged}</h2></div>`].join("")+p.standardization.samples.map(s=>`<div class="card"><b>${esc(s.canonical)}</b><p>${s.count} 个叫法</p><p>${s.examples.map(esc).join(" / ")}</p></div>`).join("");
 $("#styleBox").innerHTML=p.styles.map(s=>`<button class="style ${s.id===p.selectedStyle?"active":""}" data-style="${s.id}"><img src="${s.sample?.url||""}"><span class="style-body"><b>${esc(s.name)}</b><span>直接复用 ${s.direct}（${s.directRate}%）</span><span>二次加工 ${s.bgReplace+s.review}（${s.processingRate}%）</span><span>需定制 ${s.custom}（${s.customRate}%）</span><strong>${s.estimatedPoints} 积分</strong></span></button>`).join("");
 document.querySelectorAll(".style").forEach(b=>b.onclick=async()=>{state.style=b.dataset.style;await load()});
 $("#summary").innerHTML=[`总数 ${p.summary.total}`,`直接 ${p.summary.direct}`,`复核 ${p.summary.review}`,`缺图 ${p.summary.missing}`,`换背景 ${p.summary.bgReplace}`,`预计 ${p.summary.points} 积分`].map(x=>`<span class="pill">${x}</span>`).join("");
 $("#resultBox").innerHTML=p.results.map(r=>{const c=r.candidates[0];return`<div class="result">${c?`<img src="${c.url}">`:`<div style="height:175px;display:grid;place-items:center;background:#f6f7f9">缺图</div>`}<div class="result-body"><b>${esc(r.name)}</b><p>${esc(r.category)} · ${esc(r.kind)}</p><div><span class="pill ${tone(r.status)}">${r.status}</span><span class="pill ${tone(r.backgroundAction)}">${r.backgroundAction}</span><span class="pill">${r.points}积分</span></div><p>${c?`${esc(c.dishName)} | ${esc(c.store)} | ${c.score}分`:"进入定制/生成池"}</p></div></div>`}).join("");
 $("#paidBox").innerHTML=`<div class="paid"><div class="price">¥${p.quote.cash}</div><b>${p.quote.package}</b><p>${p.quote.points} 积分 · ${p.quote.rate}</p></div>`+p.quote.addOns.map(a=>`<div class="paid"><b>${esc(a.name)}</b><p>¥${a.price}</p></div>`).join("")+`<div class="paid"><b>邀请奖励</b><p>注册送 ${p.quote.referral.registerReward} 积分；首付 ${p.quote.referral.firstPayReward}</p></div>`;
}
async function load(){state.plan=await api(`/api/plan?style=${encodeURIComponent(state.style)}`);state.style=state.plan.selectedStyle;render()}
async function upload(id,url){const f=$(id).files[0];if(!f)return toast("请先选择文件");const fd=new FormData();fd.append("file",f);toast("上传中");const d=await api(url,{method:"POST",body:fd});state.plan=d.plan;state.style=d.plan.selectedStyle;render();toast("上传完成")}
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab,.view").forEach(x=>x.classList.remove("active"));b.classList.add("active");$("#"+b.dataset.view).classList.add("active")});
$("#uploadMenuBtn").onclick=()=>upload("#menuFile","/api/upload-menu");$("#uploadLibraryBtn").onclick=()=>upload("#libraryFile","/api/upload-library");
$("#exportBtn").onclick=async()=>{const d=await api("/api/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({style:state.style,scope:$("#scopeSelect")?.value||"all"})});toast(`已导出 ${d.rows} 条`);location.href=d.download};
load().catch(e=>toast(e.message));
