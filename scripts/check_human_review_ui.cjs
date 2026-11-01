// SYNTHETIC offline controls for the actual fixed-review and four-axis Vue components.
const assert = require("node:assert/strict");
const fs = require("node:fs"); const path = require("node:path"); const vm = require("node:vm");
const root = path.resolve(__dirname, ".."); const deps = path.join(root,"apps/web/node_modules");
const Vue = require(path.join(deps,"vue")); const {renderToString}=require(path.join(deps,"@vue/server-renderer"));
const ts=require(path.join(deps,"typescript"));
const loader=require("./check_query_policy_ui.cjs"); const api=loader.loadApi("apps/web/src/api/humanReview.ts");
const axes=loader.compileComponent("apps/web/src/components/engine/AssessmentAxesPanel.vue",{"../../api/humanReview":api});
const summary=loader.compileComponent("apps/web/src/components/engine/HumanReviewSummary.vue",{"../../api/humanReview":api,"./AssessmentAxesPanel.vue":{default:axes}});
async function main(){
 const a={id:"a",investigation_id:"i",state:"needs_review",result:{report:{final_text:"SYNTHETIC exact body",text_sha256:"b".repeat(64)},claims:[]}};
 const rows=[{entry_id:"review-1",review_revision:3,reviewer_actor:"SYNTHETIC human",reviewed_at:"2026-10-31T10:00:00Z",binding_status:"exact_target",decision:"needs_verification",scope:"SYNTHETIC selected scope",text:"SYNTHETIC still needs checking",conditions:"SYNTHETIC limits",target:{type:"report",analysis_id:"a",text_sha256:"b".repeat(64)},basis_record_refs:[]}];
 a.human_review_context={status:"recorded",analysis_id:"a",reviews:rows,investigation_only_judgment_count:0};
 let html=await renderToString(Vue.createSSRApp(summary,{analysis:a,entries:[]}));
 assert.match(html,/待人工核实.*匹配本版固定对象/); assert.match(html,/SYNTHETIC human/); assert.match(html,/SYNTHETIC selected scope/);assert.doesNotMatch(html,/已批准警报|正式警报已发布/);
 rows[0]={...rows[0],binding_status:"target_changed",decision:"accepted_within_scope"}; a.human_review_context.status="not_reviewed";
 html=await renderToString(Vue.createSSRApp(summary,{analysis:a,entries:[]}));
 assert.match(html,/目标已变化，旧审不覆盖本版/);assert.match(html,/尚无针对本稿固定对象的人工核实记录/);
 html=await renderToString(Vue.createSSRApp(axes,{axes:{source_reliability:[{grade:"F",assessment_status:"not_assessed"}],information_credibility:[{grade:"6",assessment_status:"not_assessed",claim_id:"source-claim"}],evidence_level:"unverified",heat:{score:9000}}}));
 assert.match(html,/来源可靠性（A–F）/);assert.match(html,/具体信息可信度（1–6）/);assert.match(html,/证据等级/);assert.match(html,/曝光热度/);assert.match(html,/未评定/);assert.match(html,/9000/);assert.match(html,/不合成信用分/);assert.doesNotMatch(html,/50%真实|评级：A|grade.*1/);
 const current={...a,human_review_context:{...a.human_review_context,status:"recorded",reviews:[{...rows[0],binding_status:"exact_target",decision:"accepted_within_scope"}]},
   result:{...a.result,report:{final_text:"SYNTHETIC final text",text_sha256:"b".repeat(64)},assessment_axes:{source_reliability:[{grade:"C",assessment_status:"human_reviewed"}],information_credibility:[],evidence_level:null,heat:null}},
   initial_draft:{claims:[],report:{final_text:"SYNTHETIC old text",text_sha256:"a".repeat(64)},assessment_axes:{source_reliability:[{grade:"A",assessment_status:"human_reviewed"}],information_credibility:[{grade:"1",assessment_status:"human_reviewed"}],heat:{score:1},evidence_level:"unverified"}}};
 const child=Vue.defineComponent({props:["analysis"],setup(props){return ()=>Vue.h("section",[Vue.h("p",props.analysis.result?.report?.final_text),Vue.h(summary,{analysis:props.analysis,entries:[]}),Vue.h(axes,{axes:props.analysis.result?.assessment_axes})]);}});
 const panelHtml=await require("./check_research_ui.cjs").renderPanelScope(null,current,child);
 assert.equal((panelHtml.match(/匹配本版固定对象/g)||[]).length,1,"final review must not sign a different initial draft");
 const draftHtml=panelHtml.slice(panelHtml.indexOf('class="initial-draft"'));
 assert.match(draftHtml,/SYNTHETIC old text/);assert.doesNotMatch(draftHtml,/匹配本版固定对象|A · 完全可靠|已有独立来源确认/);
 const claim={id:"c1",text:"SYNTHETIC 🧬",conditions:["one"],evidence_links:[]};
 const digest=await api.claimDigest(claim); const crypto=require("node:crypto");
 assert.equal(digest,crypto.createHash("sha256").update(JSON.stringify({conditions:["one"],evidence_links:[],id:"c1",text:"SYNTHETIC 🧬"})).digest("hex"));
 const writes=[]; const material={id:"m1",revision:2,payload:{kind:"material",text:"SYNTHETIC basis",support:{record_ref:{provider:"fixture",object_id:"fixed",adopted_version:"v1"}}}};
 const props=Vue.reactive({investigation:{id:"i",status:"open",revision:2,origin:null,entries:[material]},reviewAnalysis:a,snapshots:[],busy:false,loading:false,sourceCursor:null,save:async body=>{writes.push(body);return true;}});
 const context={exports:{},console,Error,defineProps:()=>props,defineEmits:()=>()=>{},require(name){if(name==="vue")return {...Vue,onUnmounted(){}};if(name.endsWith("/api/humanReview"))return api;if(name.endsWith("/api/engine"))return {engineRequest(){throw new Error("offline form cannot use HTTP");}};return {};}};
 const source=fs.readFileSync(path.join(root,"apps/web/src/components/engine/InvestigationEntryForm.vue"),"utf8");
 const code=ts.transpileModule(source.slice(source.indexOf(">")+1,source.indexOf("</script>")),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInNewContext(code+"\nglobalThis.probe={targetOptions,reviewTargetKey,reviewScope,judgmentText,basisIds,rateSource,ratingRecordId,sourceGrade,publisherScope,ratingBasis,addJudgment,reviewError};",context);
 await Vue.nextTick(); await Promise.resolve();const form=context.probe;
 form.reviewTargetKey.value="analysis-0";form.judgmentText.value="SYNTHETIC human note";
 await form.addJudgment();assert.equal(writes.length,0);assert.ok(form.reviewError.value);
 form.reviewScope.value="SYNTHETIC report only";form.basisIds.value=["m1"];form.rateSource.value=true;form.ratingRecordId.value="m1";form.sourceGrade.value="C";form.publisherScope.value="SYNTHETIC publisher";form.ratingBasis.value="SYNTHETIC source history";
 props.reviewAnalysis={...a,result:{...a.result}};await Vue.nextTick();assert.equal(form.reviewTargetKey.value,"analysis-0","same-result poll must not reset a human review draft");
 await form.addJudgment();assert.equal(writes.length,1);assert.equal(writes[0].review.target.analysis_id,"a");assert.equal(writes[0].review.decision,"needs_verification");assert.equal(writes[0].assessment_axes.source_reliability[0].grade,"C");assert.equal(writes[0].assessment_axes.source_reliability[0].assessed_by,undefined);
 props.reviewAnalysis={...a,id:"changed-analysis"};await Vue.nextTick();form.judgmentText.value="SYNTHETIC draft kept";await form.addJudgment();assert.equal(writes.length,1,"changed target must require reselection, not silently become a general judgment");
 console.log(JSON.stringify({status:"passed",checks:["native-fixed-review-context-rendered","old-review-does-not-cover-changed-target","four-axes-F6-not-false-and-heat-not-credit","claim-canonical-sha","actual-review-form-sends-fixed-target-and-explicit-rating-without-self-authority","same-result-poll-preserves-draft-and-changed-target-requires-reselection"]}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
