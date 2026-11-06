export class PliqraError extends Error { status:number; detail:unknown; }
export class ContractChanged extends PliqraError {}
export type Message={role:'system'|'developer'|'user'|'assistant'|'tool'|'function';content?:unknown;[key:string]:unknown};
export type Binding={id:string;service:string;fingerprint:string;expires:number;invoke:string;extensions:string[]};
export class Pliqra {
  constructor(baseURL:string,apiKey:string,options?:{fetch?:typeof fetch;timeout?:number});
  request(path:string,body?:unknown,options?:{signal?:AbortSignal}):Promise<Response>;
  json(path:string,body?:unknown,options?:{signal?:AbortSignal}):Promise<any>;
  metadata():Promise<any>;
  services():Promise<any[]>;
  connect(service:string,options?:{requirements?:string[];ttlSeconds?:number;requiredExtensions?:string[];optionalExtensions?:string[]}):Promise<Connection>;
}
export class Connection {
  binding:Binding;
  complete(messages:Message[],parameters?:Record<string,unknown>):Promise<any>;
  operation(operation:'embeddings'|'speech'|'transcription',parameters?:Record<string,unknown>):Promise<any|Uint8Array>;
  batch(items:Record<string,unknown>[],options?:{name?:string;storeOutputs?:boolean}):Promise<any>;
  stream(messages:Message[],parameters?:Record<string,unknown>,options?:{signal?:AbortSignal}):AsyncGenerator<any>;
}
