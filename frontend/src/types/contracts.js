/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,name:string,source_dataset_id:number,base_checksum:string,finance_revision:number,channel_revision:number,case_version:number,status:('open'|'ready'|'closed'),published_dataset_id:number|null}} ReconciliationCase */
/** @typedef {{id:number,case_id:number,status:string,finance_revision:number,channel_revision:number,case_version:number,stats:Object,digest:string,error:string}} ReconciliationMerge */
/** @typedef {{row:{day:string,channel:string},field:(string|null),kind:('cell'|'delete_modify'),base:*,finance:*,channel:*}} ReconciliationConflict */
export {};
