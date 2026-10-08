import { readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { canonicalText, codePointCompare, JsonObject, JsonValue, resultToText, snapshot } from './identity.js';
import { IntegrityError, MissingNodeError, Node, NodeKind, Store, validateNode } from './tree.js';
import { AtomicStore, MaintenanceStore, sameIdentity } from './stores.js';
import { seal, SealReport } from './seal.js';

export const EXPORT_FORMAT='pollard/subtree/v1';
export const JSONL_FORMAT='pollard/jsonl/v1';
export interface GCReport { mode:'drop-pruned'|'compact'; removedNodes:string[]; removedBlobs:number; survivorSeals:Record<string,string>; }
export interface ExportReport { path:string; rootId:string; digest:string; nodes:number; }
export interface ImportReport { path:string; rootId:string; digest:string; imported:number; existing:number; }
export interface MergeReport { copied:number; existing:number; resultConflicts:number; metaConflicts:number; }
interface WireNode { id:string; parent:string|null; kind:NodeKind; attempt:number; payload:string; result:string|null; result_digest:string|null; meta:string; }
function wireNode(node:Node):WireNode { validateNode(node); return {id:node.id,parent:node.parent,kind:node.kind,attempt:node.attempt,payload:canonicalText(node.payload),result:node.resultText,result_digest:node.resultDigest,meta:resultToText(node.meta)}; }
function parseNode(value:unknown):Node {
  if(!value || typeof value!=='object' || Array.isArray(value)) throw new IntegrityError('subtree node record must be an object');
  const row=value as Record<string,unknown>;
  for(const key of ['id','kind','payload','meta']) if(typeof row[key]!=='string') throw new IntegrityError(`subtree node ${key} must be a string`);
  for(const key of ['parent','result','result_digest']) if(row[key]!==null && typeof row[key]!=='string') throw new IntegrityError(`subtree node ${key} must be a string or null`);
  if(typeof row.attempt!=='number' || !Number.isSafeInteger(row.attempt)) throw new IntegrityError('subtree node attempt must be an integer');
  try { return Node.fromStorage({ id:row.id as string,parent:row.parent as string|null,kind:row.kind as NodeKind,attempt:row.attempt,payload:JSON.parse(row.payload as string),result_text:row.result as string|null,result_digest:row.result_digest as string|null,meta:JSON.parse(row.meta as string) }); }
  catch(cause) { throw new IntegrityError(`invalid subtree node record: ${cause instanceof Error?cause.message:String(cause)}`); }
}
function atomic<T>(store:Store, operation:()=>T):T {
  const transactional=store as Partial<AtomicStore>;
  if(typeof transactional.transaction==='function') return transactional.transaction(operation);
  // A seven-method third-party store has no rollback capability. Validation is
  // still complete before mutation, but backend failures cannot be rolled back.
  return operation();
}
function writeAtomic(path:string,text:string):void {
  const temporary=path+'.'+randomUUID()+'.tmp';
  try { writeFileSync(temporary,text,{encoding:'utf8',flag:'wx'}); renameSync(temporary,path); }
  finally { try { unlinkSync(temporary); } catch(error) { if((error as NodeJS.ErrnoException).code!=='ENOENT') throw error; } }
}
function rootsSeals(store:Store):Record<string,string> { return Object.fromEntries(store.roots().map(root=>[root,seal(store,root).digest])); }
export function gc(store:Store,options:{mode?:'drop-pruned'|'compact'}={}):GCReport {
  const mode=options.mode??'drop-pruned',maintenance=store as Partial<MaintenanceStore>;
  if(mode!=='drop-pruned' && mode!=='compact') throw new TypeError(`unsupported gc mode: ${mode}`);
  if(typeof maintenance.dropNodes!=='function' || typeof maintenance.compact!=='function') throw new TypeError('store backend does not support offline garbage collection');
  return atomic(store,()=>{
    rootsSeals(store);
    const removed=new Set<string>();
    let removedBlobs=0;
    if(mode==='drop-pruned') {
      for(const root of store.roots()) for(const node of store.walk(root)) if(!removed.has(node.id) && node.meta.pruned===true) for(const child of store.walk(node.id)) removed.add(child.id);
      maintenance.dropNodes!(removed);
    } else removedBlobs=maintenance.compact!();
    return {mode,removedNodes:[...removed].sort(codePointCompare),removedBlobs,survivorSeals:rootsSeals(store)};
  });
}
export function exportSubtree(store:Store,rootId:string,path:string):ExportReport {
  const report=seal(store,rootId),nodes=[...store.walk(rootId)];
  const staged=new ManifestStore(rootId,nodes);
  if(resultToText(seal(staged,rootId) as unknown as JsonValue)!==resultToText(report as unknown as JsonValue)) throw new IntegrityError('subtree changed while exporting');
  writeAtomic(path,JSON.stringify({format:EXPORT_FORMAT,root_id:rootId,seal:report,nodes:nodes.map(wireNode)},null,2)+'\n');
  return {path,rootId,digest:report.digest,nodes:nodes.length};
}
export function importSubtree(path:string,store:Store):ImportReport {
  let manifest:unknown;
  try { manifest=JSON.parse(readFileSync(path,'utf8')); } catch(cause) { if(cause instanceof SyntaxError) throw new IntegrityError('invalid subtree JSON'); throw cause; }
  if(!manifest || typeof manifest!=='object' || Array.isArray(manifest)) throw new IntegrityError('unsupported subtree export format');
  const value=manifest as Record<string,unknown>;
  if(value.format!==EXPORT_FORMAT || typeof value.root_id!=='string' || !Array.isArray(value.nodes) || !value.seal || typeof value.seal!=='object') throw new IntegrityError('invalid subtree manifest');
  return importVerified(path,value.root_id,value.nodes.map(parseNode),value.seal as SealReport,store);
}
function importVerified(path:string,rootId:string,nodes:Node[],expected:SealReport,store:Store):ImportReport {
  const staged=new ManifestStore(rootId,nodes),actual=seal(staged,rootId);
  if(resultToText(actual as unknown as JsonValue)!==resultToText(expected as unknown as JsonValue)) throw new IntegrityError('subtree seal does not match the manifest');
  return atomic(store,()=>{
    const root=staged.get(rootId);
    if(root.parent!==null && !store.exists(root.parent)) throw new IntegrityError(`subtree parent is missing from target store: ${root.parent}`);
    let existing=0;
    for(const node of nodes) if(store.exists(node.id)) {
      const old=store.get(node.id); validateNode(old);
      if(!sameIdentity(old,node)) throw new IntegrityError(`target identity conflicts with imported node: ${node.id}`);
      if(old.resultText!==node.resultText || old.resultDigest!==node.resultDigest) throw new IntegrityError(`target result conflicts with imported node: ${node.id}`);
      existing++;
    }
    for(const node of nodes) if(!store.exists(node.id)) store.put(node);
    return {path,rootId,digest:actual.digest,imported:nodes.length-existing,existing};
  });
}
/** JSONL envelope retains the Python subtree wire records and complete seal. */
export function exportJSONL(store:Store,rootId:string,path:string):ExportReport {
  const nodes=[...store.walk(rootId)],report=seal(new ManifestStore(rootId,nodes),rootId);
  writeAtomic(path,[JSON.stringify({format:JSONL_FORMAT,root_id:rootId,seal:report}),...nodes.map(node=>JSON.stringify(wireNode(node)))].join('\n')+'\n');
  return {path,rootId,digest:report.digest,nodes:nodes.length};
}
export function importJSONL(path:string,store:Store):ImportReport {
  let records:unknown[];
  try { const lines=readFileSync(path,'utf8').split(/\r?\n/); if(lines.at(-1)==='') lines.pop(); records=lines.map(line=>JSON.parse(line)); }
  catch(cause) { if(cause instanceof SyntaxError) throw new IntegrityError('invalid JSONL recording'); throw cause; }
  const header=records.shift() as Record<string,unknown>;
  if(!header || header.format!==JSONL_FORMAT || typeof header.root_id!=='string' || !header.seal) throw new IntegrityError('unsupported JSONL recording format');
  return importVerified(path,header.root_id,records.map(parseNode),header.seal as SealReport,store);
}
export function merge(destination:Store,source:Store,options:{replay?:boolean;requireAtomic?:boolean}={}):MergeReport {
  if(options.requireAtomic && typeof (destination as Partial<AtomicStore>).transaction!=='function') throw new TypeError('destination does not support atomic merge');
  const nodes:Node[]=[],seen=new Set<string>();
  for(const root of source.roots()) {
    let first=true;
    for(const original of source.walk(root)) {
      validateNode(original); const node=Node.fromStorage(original.toStorage());
      if(first && node.id!==root) throw new IntegrityError('merge source traversal does not begin with its root');
      if(first && node.parent!==null) throw new IntegrityError('merge source root has a parent');
      first=false;
      if(seen.has(node.id)) continue;
      if(node.parent!==null && !seen.has(node.parent)) throw new IntegrityError('merge source traversal yielded a node before its parent');
      seen.add(node.id); nodes.push(node);
    }
    if(first) throw new IntegrityError('merge source traversal yielded no root');
  }
  return atomic(destination,()=>{
    for(const incoming of nodes) if(destination.exists(incoming.id)) {
      const existing=destination.get(incoming.id); validateNode(existing);
      if(!sameIdentity(existing,incoming)) throw new IntegrityError('node id collision');
      if(options.replay && incoming.resultText!==null && incoming.resultText!==existing.resultText) throw new IntegrityError(`result collision during replay merge: ${incoming.id}`);
    }
    const report:MergeReport={copied:0,existing:0,resultConflicts:0,metaConflicts:0};
    for(const incoming of nodes) {
      if(!destination.exists(incoming.id)) { destination.put(incoming); report.copied++; continue; }
      report.existing++;
      const existing=destination.get(incoming.id),[meta,conflicts]=mergeMeta(existing.meta,incoming.meta);
      report.metaConflicts+=conflicts;
      if(incoming.resultText!==null && incoming.resultText!==existing.resultText) {
        const prior=list(meta.result_conflicts),updated=union(prior,[{result_digest:incoming.resultDigest,result:incoming.result}]);
        if(updated.length>prior.length) report.resultConflicts++;
        meta.result_conflicts=updated;
      }
      if(resultToText(meta)!==resultToText(existing.meta)) destination.updateMeta(existing.id,meta);
    }
    return report;
  });
}
function list(value:JsonValue|undefined):JsonValue[] { return Array.isArray(value)?value:[]; }
function union(a:JsonValue[],b:JsonValue[]):JsonValue[] { const values=new Map([...a,...b].map(value=>[resultToText(value),value])); return [...values.keys()].sort(codePointCompare).map(key=>values.get(key)!); }
function mergeMeta(existing:JsonObject,incoming:JsonObject):[JsonObject,number] {
  const merged=snapshot(existing),recorded=union(list(existing.merge_conflicts),list(incoming.merge_conflicts)),conflicts:JsonValue[]=[];
  const visit=(a:JsonValue,b:JsonValue,path:string[]):JsonValue=>{
    if(resultToText(a)===resultToText(b)) return a;
    if(a && b && typeof a==='object' && typeof b==='object' && !Array.isArray(a) && !Array.isArray(b)) {
      const value=snapshot(a);
      for(const key of Object.keys(b).sort(codePointCompare)) Object.defineProperty(value,key,{value:Object.hasOwn(value,key)?visit(value[key],b[key],[...path,key]):snapshot(b[key]),enumerable:true,writable:true,configurable:true});
      return value;
    }
    if(Array.isArray(a) && Array.isArray(b)) return union(a,b);
    conflicts.push({path:path.join('.'),values:union([a],[b])}); return a;
  };
  for(const key of Object.keys(incoming).filter(key=>key!=='merge_conflicts').sort(codePointCompare)) Object.defineProperty(merged,key,{value:Object.hasOwn(merged,key)?visit(merged[key],incoming[key],[key]):snapshot(incoming[key]),enumerable:true,writable:true,configurable:true});
  const updated=union(recorded,conflicts); if(updated.length) merged.merge_conflicts=updated;
  return [merged,updated.length-recorded.length];
}
class ManifestStore implements Store {
  readonly #nodes=new Map<string,Node>();
  constructor(readonly rootId:string,nodes:Node[]) {
    if(!nodes.length || nodes[0].id!==rootId) throw new IntegrityError('subtree manifest does not begin with its root');
    const ids=new Set(nodes.map(node=>node.id));
    if(ids.size!==nodes.length) throw new IntegrityError('subtree manifest contains duplicate node ids');
    if(nodes[0].parent!==null && ids.has(nodes[0].parent)) throw new IntegrityError('subtree root parent must be outside the manifest');
    nodes.forEach((node,index)=>{ validateNode(node); if(index && (node.parent===null || !this.#nodes.has(node.parent))) throw new IntegrityError('subtree node parent is outside the manifest'); this.#nodes.set(node.id,node); });
    if([...this.walk(rootId)].some((node,index)=>node.id!==nodes[index].id)) throw new IntegrityError('subtree nodes are not in deterministic walk order');
  }
  put(_node:Node):void { throw new TypeError('manifest store is read-only'); }
  get(id:string):Node { const node=this.#nodes.get(id); if(!node) throw new MissingNodeError(id); return node; }
  exists(id:string):boolean { return this.#nodes.has(id); }
  children(id:string):string[] { return [...this.#nodes.values()].filter(node=>node.parent===id).sort((a,b)=>codePointCompare(a.kind,b.kind)||codePointCompare(a.id,b.id)).map(node=>node.id); }
  updateMeta(_id:string,_patch:JsonObject):void { throw new TypeError('manifest store is read-only'); }
  *walk(rootId:string):Iterable<Node> { const pending=[rootId]; while(pending.length) {const id=pending.pop()!; yield this.get(id); pending.push(...this.children(id).reverse());} }
  roots():string[] { return [this.rootId]; }
}
